"""Candidate proposal strategies and Wiki evidence coordination."""

from __future__ import annotations

import json
import os
import time

from darwinagent.agents.protocol import ProtocolError
from darwinagent.contracts import AnswerResult, EvaluationResult, RunResult
from darwinagent.kernel import KernelBundle
from darwinagent.kernel.validation import capability_names
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.deadline import RoundDeadlineExceeded
from darwinagent.runtime.workspace import Workspace

from .constants import ADMISSION_ATTEMPTS
from .feedback import (
    _per_case_feedback_facts,
    _wiki_training_evidence,
    pipeline_active_stages,
    question_identity,
    training_feedback,
)
from .proposal import ProposalGenerator
from .recovery import (
    _failed_tool_params,
)
from .wiki_evidence import asset_evidence, safe_feedback, safe_scores


class WikiAdmissionExhausted(ValueError):
    pass


def _wiki_report_valid(report, candidate, *, smoke=False, config, dynamic_trial, snapshot_root):
    if (
        report.get("verdict") != "passed"
        or report.get("candidate_version") != candidate.version
        or report.get("config_digest") != digest(config.to_dict())
        or report.get("asset_fingerprints")
        != {a.id: a.fingerprint for a in candidate.assets.assets}
    ):
        return False
    if snapshot_root is not None:
        from .snapshots import snapshot_digest

        if any(
            snapshot_digest(snapshot_root / case_id) != saved
            for case_id, saved in report.get("snapshot_digests", {}).items()
        ):
            return False
        if not report.get("snapshot_digests"):
            return False
    elif dynamic_trial:
        # 动态图模式的报告必须绑定真图（graph_digests 由 admit_candidate 按
        # 排序行 digest 写入）；无图绑定的报告不得作为有效准入证据复用。
        if not report.get("graph_digests"):
            return False
    return not smoke or report.get("smoke", {}).get("status") == "passed"


async def _wiki_bootstrap_trials(wiki, *, root):
    for report_file in sorted((root / "B0" / "bootstrap-trials").glob("*.json")):
        report = json.loads(report_file.read_text())
        await wiki.record("B0", "bootstrap_trial", report, source=str(report_file), scope="trial")


def _human_objective(workspace, branch="main"):
    for event in reversed(workspace.events(kind="human_intervention")):
        if event["payload"].get("branch", "main") != branch:
            continue
        change = workspace.read_json(event["payload"]["draft_ref"])
        if change.get("kind") in ("objective", "goal"):
            goal = change.get("objective", change.get("goal", change.get("direction")))
            if goal is not None:
                return goal
    return None


async def _wiki_attempt(
    wiki,
    stage,
    name,
    cases,
    spec,
    adopted,
    results,
    scope,
    *,
    client_factory,
    preflight_hook,
    record_smoke_hook,
    get_round_deadline,
    smoke_gate_hook,
    valid_proposal_raw_hook,
    wiki_report_valid_hook,
    config,
    dynamic_trial,
    proposal_attempts,
    revisions,
    round_deadline_s,
    snapshot_root,
    branch="main",
):
    """Resume at the first uncompleted attempt; never reuse an uncertain model call."""
    from darwinagent.agents.protocol import parse_json

    required_caps = capability_names(getattr(spec, "retrieval_floor", {}) or {})
    questions = [
        {"training_id": tid, "text": q.text}
        for case in cases
        for tid, q in zip(question_identity(case), case.questions)
    ]
    training_ids = [tid for case in cases for tid in question_identity(case)]
    forbidden = [q.text for case in cases for q in case.questions]
    candidate_path = stage / "candidate" / "bundle"
    goal_path = stage / "optimization" / "goal.json"
    if not goal_path.exists():
        entries = wiki._wiki()["entries"]
        origin = next(
            (
                e
                for e in reversed(entries)
                if e.get("attribution") and e["kind"] in ("formal", "decision")
            ),
            None,
        )
        manual_goal = _human_objective(wiki.service.workspace, branch)
        atomic_json(
            goal_path,
            {
                "base_version": adopted.version,
                "stage": name,
                "direction": manual_goal
                if manual_goal is not None
                else (
                    origin["attribution"]["action"]
                    if origin
                    else "根据训练证据修复未解决问题，可联合调整 S/F/C/P；不降低验收门槛。"
                ),
                "objective_source": "human" if manual_goal is not None else "wiki",
                "evidence_ids": [origin["id"]] if origin else [],
            },
        )
    goal = json.loads(goal_path.read_text())
    for attempt in range(proposal_attempts):
        # 轮预算（新模式，recheck4）：deadline 自 round 起不随 attempt 重置；
        # 到点终止在途工作并持久化状态，由上层记 round_timeout（超时轮
        # 不计正式轮）。旧模式 round_deadline_s=None，检查为零成本短路。
        if get_round_deadline() is not None and time.monotonic() > get_round_deadline():
            record_dir = stage / "optimization" / f"attempt-{attempt}"
            atomic_json(
                record_dir / "status.json",
                {
                    "state": "deadline",
                    "attempt": attempt,
                    "base_version": adopted.version,
                    "error": f"round deadline exceeded ({round_deadline_s}s)",
                    "deadline_elapsed_s": round(
                        time.monotonic() - (get_round_deadline() - round_deadline_s), 1
                    ),
                },
            )
            raise RoundDeadlineExceeded(
                f"Round deadline ({round_deadline_s}s) exceeded at attempt {attempt}"
            )
        record_dir = stage / "optimization" / f"attempt-{attempt}"
        record_path = record_dir / "status.json"
        proposal_path = record_dir / "proposal-call.json"
        staged_path = stage / f".candidate-attempt-{attempt}"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        if record and record["state"] == "no_change":
            raise WikiAdmissionExhausted("Proposal explicitly requested no change")
        if record and record["state"] == "uncertain":
            # A recovered response may now be present; the dialogue itself makes
            # the receipt check and never sends a replacement automatically.
            if not proposal_path.exists():
                raise ProtocolError("Proposal request outcome unknown; recover it explicitly")
        if record and record["state"] == "failed":
            continue
        if (
            record
            and record["state"] == "passed"
            and candidate_path.joinpath("manifest.json").exists()
        ):
            await wiki.record(
                name,
                "attempt",
                {
                    "status": "passed",
                    "attempt": attempt,
                    "candidate_version": record["candidate_version"],
                    "base_version": adopted.version,
                    "asset_kinds": record.get("asset_kinds", []),
                },
                category="strategy",
                scope="smoke" if snapshot_root else "admission",
                source=str(record_path),
            )
            return KernelBundle(candidate_path)
        if not record:
            context = wiki.context()
            context["objective"] = goal
            record = {
                "state": "reserved",
                "attempt": attempt,
                "base_version": adopted.version,
                "wiki_version": context["version"],
                "wiki_context": context,
                "target": str(staged_path),
            }
            atomic_json(record_path, record)
        elif record["base_version"] != adopted.version:
            raise ValueError("Wiki attempt baseline identity mismatch")
        if record["state"] == "passed":
            raise ValueError("Published Wiki candidate is missing; refusing replay")
        try:
            if (staged_path / "bundle" / "manifest.json").exists():
                patches = ()
            elif proposal_path.exists() and "phase" not in json.loads(proposal_path.read_text()):
                saved = json.loads(proposal_path.read_text())
                if saved["input"]["base_version"] != adopted.version or (
                    saved["input"].get("wiki", {}).get("version") != record["wiki_version"]
                ):
                    raise ValueError("Saved proposal identity mismatch")
                patches = next(
                    (
                        ProposalGenerator.decode(parse_json(raw), adopted)
                        for raw in reversed(saved["raw_outputs"])
                        if raw and valid_proposal_raw_hook(raw, adopted)
                    ),
                    None,
                )
                if patches is None:
                    record.update(state="uncertain", error="No validated saved proposal")
                    atomic_json(record_path, record)
                    continue
            else:
                if (
                    not proposal_path.exists()
                    and record_path.exists()
                    and record.get("request_started")
                ):
                    record.update(state="uncertain", error="Proposal reply not persisted")
                    atomic_json(record_path, record)
                    continue
                client = client_factory(name)
                try:
                    record["request_started"] = True
                    atomic_json(record_path, record)
                    previous = stage / "optimization" / f"attempt-{attempt - 1}"
                    prior_status = previous / "status.json"
                    prior_error = (
                        json.loads(prior_status.read_text()).get("error")
                        if prior_status.exists()
                        else None
                    )
                    patches = await ProposalGenerator().propose(
                        adopted,
                        cases,
                        None,
                        client,
                        config,
                        proposal_path,
                        questions,
                        allowed_kinds=tuple(scope or ()),
                        wiki_context=record["wiki_context"],
                        wiki_service=wiki.service,
                        call_limit=getattr(config, "proposal_call_limit", None),
                        previous_session=previous / "proposal-call.json",
                        admission_error=prior_error,
                    )
                finally:
                    await client.aclose()
            if not patches and not (staged_path / "bundle" / "manifest.json").exists():
                record.update(
                    state="no_change",
                    reason=json.loads(proposal_path.read_text()).get("action", {}).get("reason"),
                )
                atomic_json(record_path, record)
                raise WikiAdmissionExhausted("Proposal explicitly requested no change")
            if patches:
                atomic_json(
                    record_dir / "patches.json", {"patches": [p.to_dict() for p in patches]}
                )
            patch_file = record_dir / "patches.json"
            _patch_evidence = json.loads(patch_file.read_text()) if patch_file.exists() else {}
            if not (staged_path / "bundle" / "manifest.json").exists():
                revisions.propose(
                    adopted,
                    patches,
                    staged_path,
                    training_ids,
                    forbidden,
                    allowed_kinds=tuple(scope or ()),
                    required_capabilities=required_caps,
                )
            staged = KernelBundle(staged_path / "bundle")
            report_path = staged_path / "admission.json"
            saved_admission = (
                json.loads((record_dir / "admission.json").read_text())
                if (record_dir / "admission.json").exists()
                else {}
            )
            try:
                if not wiki_report_valid_hook(saved_admission, staged):
                    await preflight_hook(
                        staged, spec, cases[0].questions[0], cases, _failed_tool_params(results)
                    )
            finally:
                if report_path.exists():
                    atomic_json(record_dir / "admission.json", json.loads(report_path.read_text()))
            if snapshot_root is not None or dynamic_trial:
                prior = (
                    json.loads((record_dir / "smoke.json").read_text())
                    if (record_dir / "smoke.json").exists()
                    else {}
                )
                if prior.get("status") == "passed" and wiki_report_valid_hook(
                    json.loads((record_dir / "admission.json").read_text()), staged
                ):
                    smoke_error = None
                else:
                    # 冒烟是单次尝试最长段：开跑前再查一次 deadline（不重置）。
                    if get_round_deadline() is not None and time.monotonic() > get_round_deadline():
                        raise RoundDeadlineExceeded(
                            f"Round deadline ({round_deadline_s}s) exceeded "
                            f"before smoke at attempt {attempt}"
                        )
                    smoke_started = time.monotonic()
                    smoke_error = await smoke_gate_hook(
                        cases, spec.with_bundle(staged), candidate=True
                    )
                    smoke = {
                        "status": "failed" if smoke_error else "passed",
                        "error": smoke_error,
                        "elapsed_s": round(time.monotonic() - smoke_started, 3),
                    }
                    atomic_json(record_dir / "smoke.json", smoke)
                    record_smoke_hook(staged, smoke_error, smoke["elapsed_s"])
                if smoke_error:
                    raise ValueError(smoke_error)
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_path, candidate_path.parent)
            record.update(
                state="passed",
                candidate_version=staged.version,
                candidate_path=str(candidate_path),
                asset_kinds=sorted({p.asset.kind for p in patches}) if patches else [],
            )
            atomic_json(record_path, record)
            await wiki.record(
                name,
                "attempt",
                {
                    "status": "passed",
                    "attempt": attempt,
                    "candidate_version": staged.version,
                    "base_version": adopted.version,
                    "asset_kinds": record["asset_kinds"],
                    **asset_evidence(adopted, staged),
                    "verification": json.loads((record_dir / "admission.json").read_text())
                    if (record_dir / "admission.json").exists()
                    else None,
                    "smoke": json.loads((record_dir / "smoke.json").read_text())
                    if (record_dir / "smoke.json").exists()
                    else None,
                },
                category="strategy",
                scope="smoke" if snapshot_root else "admission",
                source=str(record_path),
            )
            return KernelBundle(candidate_path)
        except WikiAdmissionExhausted:
            raise
        except (ValueError, ProtocolError) as exc:
            if isinstance(exc, ProtocolError) and (
                "outcome unknown" in str(exc) or "budget exhausted" in str(exc)
            ):
                record.update(
                    state="uncertain" if "outcome unknown" in str(exc) else "paused", error=str(exc)
                )
                atomic_json(record_path, record)
                raise
            if record.get("state") == "passed":
                raise
            record.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            atomic_json(record_path, record)
            facts = {
                "status": "failed",
                "attempt": attempt,
                "error": record["error"],
                "admission": json.loads((record_dir / "admission.json").read_text())
                if (record_dir / "admission.json").exists()
                else None,
                "smoke": json.loads((record_dir / "smoke.json").read_text())
                if (record_dir / "smoke.json").exists()
                else None,
                "patches": json.loads((record_dir / "patches.json").read_text()).get("patches", [])
                if (record_dir / "patches.json").exists()
                else [],
                "base_version": adopted.version,
                "objective": goal,
            }
            await wiki.record(
                name,
                "attempt",
                facts,
                category="runtime",
                scope="trial",
                training_ids=training_ids,
                source=str(record_path),
                infer=wiki.new_failure(facts),
            )
    raise WikiAdmissionExhausted(f"All {proposal_attempts} Wiki admission attempts failed")


def _valid_proposal_raw(raw, base=None):
    try:
        from darwinagent.agents.protocol import parse_json

        ProposalGenerator.decode(parse_json(raw), base)
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _wiki_decision_facts(decision):
    facts = {
        k: decision.get(k)
        for k in ("accepted", "status", "reasons", "base_version", "candidate_version")
    }
    if decision.get("baseline") is not None:
        facts["baseline"] = safe_scores(decision["baseline"])
    if decision.get("candidate") is not None:
        facts["candidate"] = safe_scores(decision["candidate"])
    return facts


async def _wiki_formal_from_disk(wiki, name, cases, scores, *, root, snapshot_root):
    if any(e["stage"] == name and e["kind"] == "formal" for e in wiki._wiki()["entries"]):
        return
    results = []
    for case in cases:
        path = root / name / "generation" / case.id / "result.json"
        saved = json.loads(path.read_text())
        saved["answers"] = tuple(AnswerResult.from_dict(row) for row in saved["answers"])
        results.append(RunResult(**saved))
    raw = training_feedback(
        cases,
        results,
        _per_case_feedback_facts(root, name, cases),
        EvaluationResult(**scores),
        active_stages=pipeline_active_stages(snapshot_root),
    )
    candidate = KernelBundle(root / name / "candidate" / "bundle")
    await wiki.record(
        name,
        "formal",
        {
            **safe_feedback(raw),
            **asset_evidence(candidate),
            **_wiki_training_evidence(
                cases, results, diagnostics=_per_case_feedback_facts(root, name, cases)
            ),
        },
        category="strategy",
        scope="formal",
        training_ids=[tid for case in cases for tid in question_identity(case)],
        source=str(root / name / "stage.json"),
    )


async def _legacy_candidate(
    stage,
    name,
    cases,
    spec,
    adopted,
    results,
    baseline,
    evidence,
    scope,
    decision_path,
    decisions,
    n,
    *,
    client_factory,
    preflight_hook,
    record_smoke_hook,
    smoke_gate_hook,
    config,
    dynamic_trial,
    revisions,
    root,
    snapshot_root,
    branch="main",
):
    """Keep the old proposal and error-feedback path unchanged for legacy runs."""
    client = client_factory(name)
    prev_decision = root / f"R{n - 1}" / "decision.json"
    previous_round = None
    if n > 0 and prev_decision.exists():
        pd = json.loads(prev_decision.read_text())
        previous_round = {
            "round": f"R{n - 1}",
            "status": pd.get("status"),
            "accepted": pd.get("accepted"),
            "reasons": (pd.get("reasons") or [])[:6],
        }
    feedback = training_feedback(
        cases,
        results,
        _per_case_feedback_facts(root, evidence, cases),
        baseline,
        active_stages=pipeline_active_stages(snapshot_root),
        previous_round=previous_round,
    )
    questions = [
        {"training_id": tid, "text": q.text}
        for case in cases
        for tid, q in zip(question_identity(case), case.questions)
    ]
    required_caps = capability_names(getattr(spec, "retrieval_floor", {}) or {})
    try:
        admission_error = None
        for attempt in range(ADMISSION_ATTEMPTS):
            attempt_path = stage / f".candidate-attempt-{attempt}"
            if attempt_path.exists():
                from uuid import uuid4

                retained = stage / "draft-history" / (f"attempt-{attempt}-" + uuid4().hex)
                retained.parent.mkdir(parents=True, exist_ok=True)
                os.replace(attempt_path, retained)
            try:
                patches = await ProposalGenerator().propose(
                    adopted,
                    cases,
                    feedback,
                    client,
                    config,
                    stage / "proposal-call.json",
                    questions,
                    allowed_kinds=tuple(scope or ()),
                    admission_error=admission_error,
                    objective=_human_objective(Workspace(stage.parent / "workspace"), branch),
                )
                if not patches:
                    decision = {
                        "accepted": False,
                        "status": "no_change",
                        "reasons": ["explicit_no_change"],
                        "base_version": adopted.version,
                        "candidate": None,
                    }
                    atomic_json(decision_path, decision)
                    decisions.append(decision)
                    return None
                training_ids = [tid for case in cases for tid in question_identity(case)]
                forbidden = [q.text for case in cases for q in case.questions]
                revisions.propose(
                    adopted,
                    patches,
                    attempt_path,
                    training_ids,
                    forbidden,
                    allowed_kinds=tuple(scope or ()),
                    required_capabilities=required_caps,
                )
                staged = KernelBundle(attempt_path / "bundle")
                await preflight_hook(
                    staged, spec, cases[0].questions[0], cases, _failed_tool_params(results)
                )
                if snapshot_root is not None or dynamic_trial:
                    smoke_started = time.monotonic()
                    round_smoke = await smoke_gate_hook(
                        cases, spec.with_bundle(staged), candidate=True
                    )
                    record_smoke_hook(staged, round_smoke, time.monotonic() - smoke_started)
                    if round_smoke:
                        raise ValueError(round_smoke)
                candidate_path = stage / "candidate" / "bundle"
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(attempt_path, candidate_path.parent)
                return KernelBundle(candidate_path)
            except (ValueError, ProtocolError) as exc:
                admission_error = (
                    f"[重试 {attempt + 1}/{ADMISSION_ATTEMPTS}] {type(exc).__name__}: {exc}"
                )
        raise ValueError(admission_error)
    except Exception as exc:
        decision = {
            "accepted": False,
            "status": "validation_failed",
            "reasons": [f"{type(exc).__name__}: {exc}"],
            "base_version": adopted.version,
            "candidate": None,
        }
        atomic_json(decision_path, decision)
        decisions.append(decision)
        print(json.dumps({"stage": name, **decision}, ensure_ascii=False), flush=True)
        return None
    finally:
        await client.aclose()
