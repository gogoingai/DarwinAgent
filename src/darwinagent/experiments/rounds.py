"""Iteration restore, budget, selection and durable adoption coordination."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from darwinagent.contracts import EvaluationResult, RunResult, plain
from darwinagent.kernel import KernelBundle
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.deadline import ROUND_DEADLINE
from darwinagent.runtime.execution import ActiveBudget
from darwinagent.runtime.workspace import SelectionConflict, Workspace

from .control import ControlSignal
from .feedback import (
    _per_case_feedback_facts,
    _wiki_training_evidence,
    pipeline_active_stages,
    question_identity,
    training_feedback,
)
from .optimization import WikiAdmissionExhausted
from .recovery import (
    _failed_tool_params,
)
from .statistics import stability_metrics
from .wiki_evidence import asset_evidence, safe_feedback


async def _wiki_record_outbox(wiki, stage, *args, **kwargs):
    """Persist maintenance independently; a failed model must not undo a decision."""
    if wiki is None:
        return
    task = {"args": list(args), "kwargs": kwargs}
    path = stage / "wiki-outbox" / (digest(task) + ".json")
    saved = json.loads(path.read_text()) if path.exists() else {}
    if saved.get("state") == "done":
        return
    atomic_json(path, {**task, "state": "pending"})
    try:
        await wiki.record(*args, **kwargs)
    except Exception as exc:
        atomic_json(path, {**task, "state": "pending", "error": f"{type(exc).__name__}: {exc}"})
    else:
        atomic_json(path, {**task, "state": "done"})


def _remember_bundle(workspace, bundle):
    atomic_json(
        workspace.root / "bundles" / (bundle.version + ".json"),
        {"version": bundle.version, "path": str(bundle.root)},
    )


def _working_bundle(workspace, selected, fallback):
    version = selected.get("working")
    if not version or version == fallback.version:
        return fallback
    direct = Path(version)
    if direct.joinpath("manifest.json").exists():
        return KernelBundle(direct)
    manual_path = workspace.root.parent / "versions" / version
    if manual_path.joinpath("manifest.json").exists():
        return KernelBundle(manual_path)
    path = workspace.root / "bundles" / (version + ".json")
    if not path.exists():
        raise ValueError("Selected working candidate is unavailable: " + version)
    return KernelBundle(json.loads(path.read_text())["path"])


def _select_candidate(workspace, branch, revision, candidate, decision, revisions, root):
    _remember_bundle(workspace, candidate)
    publication = root / "publication-outbox" / (candidate.version + ".json")
    atomic_json(
        publication,
        {
            "state": "pending",
            "branch": branch,
            "revision": revision,
            "candidate_version": candidate.version,
            "accepted": decision["accepted"],
        },
    )
    try:
        selected = workspace.publish_selection(
            branch,
            expected_revision=revision,
            working=candidate.version,
            adopted=candidate.version if decision["accepted"] else None,
            publish=(lambda: revisions.publish(candidate, root / "published", decision))
            if decision["accepted"]
            else None,
        )
    except SelectionConflict:
        atomic_json(
            publication,
            {
                "state": "detached",
                "branch": branch,
                "revision": revision,
                "candidate_version": candidate.version,
            },
        )
        decision["selection_status"] = "detached_after_human_change"
        return False
    atomic_json(
        publication,
        {
            "state": "done",
            "branch": branch,
            "revision": selected["revision"],
            "candidate_version": candidate.version,
        },
    )
    decision["selection_status"] = "selected"
    return True


@dataclass(frozen=True)
class _IterationState:
    """Accepted in-memory evidence; never serialized as a new artifact schema."""

    adopted: KernelBundle
    baseline: EvaluationResult
    results: list[RunResult]
    evidence_stage: str
    validation_baseline: EvaluationResult | None = None


async def run_rounds(
    cases,
    spec,
    wiki,
    adopted,
    baseline,
    results,
    val_case,
    val_baseline,
    val_history,
    rounds,
    stop_file,
    stage_gate,
    scope,
    *,
    legacy_candidate_hook,
    preflight_hook,
    record_smoke_hook,
    get_round_deadline,
    smoke_gate_hook,
    stage_hook,
    stage_health_hook,
    wiki_attempt_hook,
    wiki_decision_facts_hook,
    wiki_formal_from_disk_hook,
    wiki_report_valid_hook,
    dynamic_trial,
    graph_builder,
    selection_policy,
    revisions,
    root,
    round_deadline_s,
    snapshot_root,
    validation_plan,
    verify,
    set_round_deadline,
    workspace=None,
    branch="main",
    control_hook=None,
    policy_hook=None,
):
    # The stage whose evaluation currently backs `baseline`/`results`: proposals must
    # read diagnostics from THERE, never from the round being proposed (it has not
    # run yet). A rejected candidate leaves it unchanged.
    workspace = workspace or Workspace(root / "workspace")
    _remember_bundle(workspace, adopted)
    try:
        selected = workspace.branch(branch)
    except KeyError:
        selected = workspace.create_branch(branch, adopted=adopted.version, working=adopted.version)
    if selected["adopted"] is None or selected["working"] is None:
        selected = workspace.select_branch(
            branch,
            expected_revision=selected["revision"],
            adopted=selected["adopted"] or adopted.version,
            working=selected["working"] or adopted.version,
        )
    state = _IterationState(adopted, baseline, results, "B0", val_baseline)
    decisions = []
    stopped = False
    control_status = None
    n = 0
    while True:
        # Recorded decisions are always restored first — a STOP signal (or a rounds
        # cap) must never truncate history that already happened.
        next_decision = root / f"R{n + 1}" / "decision.json"
        if next_decision.exists():
            n += 1
            verify()
            name = f"R{n}"
            stage = root / name
            decision = json.loads(next_decision.read_text())
            decisions.append(decision)
            if decision.get("candidate_version"):
                publication = (
                    root / "publication-outbox" / (decision["candidate_version"] + ".json")
                )
                pending_publication = (
                    json.loads(publication.read_text()) if publication.exists() else {}
                )
                if pending_publication.get("state") == "pending":
                    restored_candidate = KernelBundle(stage / "candidate" / "bundle")
                    latest = workspace.branch(branch)
                    if latest["revision"] == pending_publication["revision"]:
                        _select_candidate(
                            workspace,
                            branch,
                            latest["revision"],
                            restored_candidate,
                            decision,
                            revisions,
                            root,
                        )
                        atomic_json(next_decision, decision)
                    elif latest["working"] == restored_candidate.version and (
                        not decision["accepted"] or latest["adopted"] == restored_candidate.version
                    ):
                        atomic_json(publication, {**pending_publication, "state": "done"})
                    else:
                        decision["selection_status"] = "detached_after_human_change"
                        atomic_json(next_decision, decision)
                        atomic_json(publication, {**pending_publication, "state": "detached"})
            if validation_plan is not None and decision.get("validation"):
                val_history[name] = decision["validation"]
                atomic_json(root / "validation.json", val_history)
            if wiki is not None:
                for pending_path in sorted((stage / "wiki-outbox").glob("*.json")):
                    pending = json.loads(pending_path.read_text())
                    if pending.get("state") == "pending" and "args" in pending:
                        await _wiki_record_outbox(
                            wiki, stage, *pending["args"], **pending["kwargs"]
                        )
                if decision.get("candidate") is not None:
                    try:
                        await wiki_formal_from_disk_hook(wiki, name, cases, decision["candidate"])
                    except Exception as exc:
                        atomic_json(
                            stage / "wiki-outbox" / "restore-formal.json",
                            {"state": "pending", "error": str(exc), "stage": name},
                        )
                formal_entry = next(
                    (
                        e
                        for e in reversed(wiki._wiki()["entries"])
                        if e["stage"] == name and e["kind"] == "formal"
                    ),
                    None,
                )
                await _wiki_record_outbox(
                    wiki,
                    stage,
                    name,
                    "decision",
                    {
                        **wiki_decision_facts_hook(decision),
                        **(formal_entry["facts"] if formal_entry else {}),
                    },
                    training_ids=[tid for case in cases for tid in question_identity(case)],
                    category="runtime"
                    if decision.get("status") in ("validation_failed", "round_timeout")
                    else "strategy",
                    scope="admission"
                    if decision.get("status") in ("validation_failed", "round_timeout")
                    else "formal",
                    source=str(next_decision),
                    infer=decision.get("status") not in ("validation_failed", "round_timeout"),
                )
            if decision["accepted"]:
                restored = KernelBundle(stage / "candidate" / "bundle")
                restored_scores = EvaluationResult(**decision["candidate"])
                restored_validation = state.validation_baseline
                if validation_plan is not None:
                    if not decision.get("validation"):
                        raise ValueError("Accepted round is missing validation scores")
                    restored_validation = EvaluationResult(**decision["validation"])
                # The durable publication pointer follows restored history before replay.
                _remember_bundle(workspace, restored)
                latest = workspace.branch(branch)
                if latest["adopted"] == restored.version:
                    workspace.publish_selection(
                        branch,
                        expected_revision=latest["revision"],
                        working=latest["working"],
                        publish=lambda restored=restored, decision=decision: revisions.publish(
                            restored, root / "published", decision
                        ),
                    )
                if stage_gate is not None:
                    stage_gate(name)
                restored_results, _ = await stage_hook(name, cases, spec.with_bundle(restored))
                state = _IterationState(
                    restored, restored_scores, restored_results, name, restored_validation
                )
            continue
        if control_hook is not None:
            try:
                control_hook("round:" + str(n + 1))
            except ControlSignal as signal:
                stopped = True
                control_status = signal.state["status"]
                break
        if stop_file is not None and Path(stop_file).exists():
            stopped = True
            break
        # 轮数口径（缺口①修复，2026-10-06）：rounds＝**完整计分轮**上限——
        # 超时/准入耗尽/服务中断的迭代如实记录但不占轮数，循环继续到凑满；
        # 2×迭代上限兜底防死循环。旧模式（无验证计划）维持迭代数口径不变。
        if rounds is not None:
            if validation_plan is None and n >= rounds:
                break
            if validation_plan is not None:
                scored = sum(1 for d in decisions if (d.get("candidate") or {}).get("metrics"))
                if scored >= rounds or n >= rounds * 2:
                    break
        n += 1
        verify()
        name = f"R{n}"
        stage = root / name
        round_started = time.time()
        budget_path = stage / "round-budget.json"
        remaining = None
        active_budget = (
            ActiveBudget(budget_path, round_deadline_s) if round_deadline_s is not None else None
        )
        if active_budget is not None:
            remaining = active_budget.remaining
            if remaining > 0:
                active_budget.__enter__()
        selected = workspace.branch(branch)
        selection_revision = selected["revision"]
        working = _working_bundle(workspace, selected, state.adopted)
        set_round_deadline(time.monotonic() + max(0, remaining) if remaining is not None else None)
        candidate_validation = state.validation_baseline
        decision_count = len(decisions)
        deadline_token = ROUND_DEADLINE.set(get_round_deadline())
        try:
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Resumed round budget exhausted")
            async with asyncio.timeout(remaining) if remaining is not None else nullcontext():
                decision_path = stage / "decision.json"
                candidate_path = stage / "candidate" / "bundle"
                if (candidate_path / "manifest.json").exists():
                    candidate = KernelBundle(candidate_path)
                    # 恢复已有候选同样过预检＋冒烟（评审三）：不能仅凭 manifest 存在就跳过验证
                    recorded = root / name / "candidate" / "admission.json"
                    saved = json.loads(recorded.read_text()) if recorded.exists() else {}
                    if not (wiki is not None and wiki_report_valid_hook(saved, candidate)):
                        await preflight_hook(
                            candidate,
                            spec,
                            cases[0].questions[0],
                            cases,
                            _failed_tool_params(state.results),
                        )
                    if (snapshot_root is not None or dynamic_trial) and not (
                        wiki is not None and wiki_report_valid_hook(saved, candidate, smoke=True)
                    ):
                        smoke_started = time.monotonic()
                        resume_smoke = await smoke_gate_hook(
                            cases, spec.with_bundle(candidate), candidate=True
                        )
                        record_smoke_hook(candidate, resume_smoke, time.monotonic() - smoke_started)
                        if resume_smoke:
                            raise ValueError("恢复候选冒烟失败: " + resume_smoke)
                else:
                    if wiki is not None:
                        try:
                            candidate = await wiki_attempt_hook(
                                wiki, stage, name, cases, spec, working, state.results, scope
                            )
                        except WikiAdmissionExhausted as exc:
                            no_change = str(exc) == "Proposal explicitly requested no change"
                            decision = {
                                "accepted": False,
                                "status": "no_change" if no_change else "validation_failed",
                                "reasons": [f"{type(exc).__name__}: {exc}"],
                                "base_version": state.adopted.version,
                                "candidate": None,
                            }
                            atomic_json(decision_path, decision)
                            decisions.append(decision)
                            await _wiki_record_outbox(
                                wiki,
                                stage,
                                name,
                                "decision",
                                wiki_decision_facts_hook(decision),
                                category="runtime",
                                scope="admission",
                                source=str(decision_path),
                            )
                            continue
                    else:
                        candidate = await legacy_candidate_hook(
                            stage,
                            name,
                            cases,
                            spec,
                            working,
                            state.results,
                            state.baseline,
                            state.evidence_stage,
                            scope,
                            decision_path,
                            decisions,
                            n,
                        )
                        if candidate is None:
                            continue
                if stage_gate is not None:
                    stage_gate(name)
                candidate_results, candidate_scores = await stage_hook(
                    name, cases, spec.with_bundle(candidate)
                )
                if control_hook is not None:
                    control_hook("selection:" + name)
                if policy_hook is not None:
                    selection_policy = policy_hook()
                decision = {
                    **selection_policy.decide(state.baseline, candidate_scores),
                    "base_version": state.adopted.version,
                    "candidate_version": candidate.version,
                }
                baseline_path = root / state.evidence_stage / "stage.json"
                candidate_stage_path = stage / "stage.json"
                baseline_record = (
                    json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
                )
                candidate_record = (
                    json.loads(candidate_stage_path.read_text())
                    if candidate_stage_path.exists()
                    else {}
                )
                baseline_criterion = baseline_record.get(
                    "criterion_id", baseline_record.get("criterion")
                )
                candidate_criterion = candidate_record.get(
                    "criterion_id", candidate_record.get("criterion")
                )
                incompatible_criteria = (
                    baseline_criterion is not None
                    and candidate_criterion is not None
                    and baseline_criterion != candidate_criterion
                )
                if (
                    baseline_record.get("comparison_reliable") is False
                    or candidate_record.get("comparison_reliable") is False
                    or incompatible_criteria
                ):
                    decision["accepted"] = False
                    decision["comparison_reliable"] = False
                    decision["reasons"] = [
                        *decision["reasons"],
                        "criterion_mismatch:尚不能按不同标准比较"
                        if incompatible_criteria
                        else "baseline_comparison_unavailable:尚不能作此比较",
                    ]
                # 新模式：P.extract 此模式不执行——其补丁不得报告为已生效优化。
                if graph_builder is not None and not getattr(
                    graph_builder, "uses_extract_prompt", False
                ):
                    origin = getattr(candidate.assets, "origin", None)
                    origin = origin if isinstance(origin, Mapping) else {}
                    px = [
                        p
                        for p in origin.get("patches", ())
                        if (p.get("asset") or {}).get("kind") == "P"
                        and (p.get("asset") or {}).get("role") == "extract"
                    ]
                    if px:
                        decision["p_extract_not_effective"] = len(px)
                # 新模式验证选版（防退化）：训练主判通过后，候选在固定验证题上聚合
                # 指标须无故障、primary 严格升＋floor 不降（与 SelectionPolicy 同谓词，
                # 逐候选判定）；验证退化即拒绝（训练小集收益只是继续迭代的信号）。
                if validation_plan is not None and decision["accepted"]:
                    _, val_scores = await stage_hook(
                        f"{name}-val", (val_case,), spec.with_bundle(candidate)
                    )
                    policy = validation_plan["policy"]
                    from .policy import split_faults

                    ext_val, det_val = split_faults(val_scores)
                    v_failures = []
                    # 外部故障不拦（操作者指令 2026-10-07）：传输/限流族留分母、
                    # 入决策披露；确定性生成故障、非故障性缺题与评测故障照拦。
                    if (
                        val_scores.total != state.validation_baseline.total
                        or val_scores.completed != val_scores.total - ext_val
                        or det_val
                    ):
                        v_failures.append("incomplete_evaluation")
                    if val_scores.evaluation_faults:
                        v_failures.append("evaluation_fault")
                    if set(val_scores.metrics) != set(state.validation_baseline.metrics):
                        v_failures.append("metric_contract_changed")
                    if not v_failures:
                        if val_scores.metrics.get(
                            policy.primary, -1
                        ) <= state.validation_baseline.metrics.get(policy.primary, -1):
                            v_failures.append("primary_not_strictly_improved")
                        if val_scores.metrics.get(
                            policy.floor, -1
                        ) < state.validation_baseline.metrics.get(policy.floor, -1):
                            v_failures.append("metric_decreased:" + policy.floor)
                    # 外部故障披露放 decision 顶层：validation 字典必须保持
                    # EvaluationResult 形状（resume 处 EvaluationResult(**d) 重建）。
                    decision["validation"] = {
                        "metrics": plain(dict(val_scores.metrics)),
                        "total": val_scores.total,
                        "completed": val_scores.completed,
                        "generation_faults": val_scores.generation_faults,
                        "evaluation_faults": val_scores.evaluation_faults,
                    }
                    if ext_val:
                        decision["validation_external_faults"] = ext_val
                    val_history[name] = val_scores.to_dict()
                    atomic_json(root / "validation.json", val_history)
                    if v_failures:
                        decision["accepted"] = False
                        decision["reasons"] = [
                            *decision["reasons"],
                            *(f"validation_{r}" for r in v_failures),
                        ]
                    else:
                        candidate_validation = val_scores
                decision["round_elapsed_s"] = round(time.time() - round_started, 1)
                atomic_json(decision_path, decision)
                decisions.append(decision)
                previous_results = state.results
                selected_now = _select_candidate(
                    workspace, branch, selection_revision, candidate, decision, revisions, root
                )
                atomic_json(decision_path, decision)
                if decision["accepted"] and selected_now:
                    state = _IterationState(
                        candidate, candidate_scores, candidate_results, name, candidate_validation
                    )
                if wiki is not None:
                    raw = training_feedback(
                        cases,
                        candidate_results,
                        _per_case_feedback_facts(root, name, cases),
                        candidate_scores,
                        active_stages=pipeline_active_stages(snapshot_root),
                    )
                    formal_facts = {
                        **safe_feedback(raw),
                        **asset_evidence(working, candidate),
                        **_wiki_training_evidence(
                            cases,
                            candidate_results,
                            previous_results,
                            _per_case_feedback_facts(root, name, cases),
                        ),
                    }
                    if decision.get("validation"):
                        formal_facts["validation"] = decision["validation"]  # 聚合指标
                    if decision.get("p_extract_not_effective"):
                        formal_facts["p_extract_not_effective"] = decision[
                            "p_extract_not_effective"
                        ]
                    await _wiki_record_outbox(
                        wiki,
                        stage,
                        name,
                        "formal",
                        formal_facts,
                        category="strategy",
                        scope="formal",
                        training_ids=[tid for case in cases for tid in question_identity(case)],
                        source=str(stage / "stage.json"),
                    )
                    await _wiki_record_outbox(
                        wiki,
                        stage,
                        name,
                        "decision",
                        {**wiki_decision_facts_hook(decision), **formal_facts},
                        training_ids=[tid for case in cases for tid in question_identity(case)],
                        category="strategy",
                        scope="formal",
                        source=str(decision_path),
                        infer=True,
                    )
                print(
                    json.dumps(
                        {
                            "stage": name,
                            "accepted": decision["accepted"],
                            "reasons": decision["reasons"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except ControlSignal as signal:
            stopped = True
            control_status = signal.state["status"]
            break
        except TimeoutError:
            if get_round_deadline() is None or time.monotonic() < get_round_deadline():
                raise
            if (stage / "decision.json").exists() and len(decisions) > decision_count:
                # The score and selection already committed. Only maintenance expired.
                atomic_json(
                    stage / "wiki-outbox" / "deadline.json",
                    {"state": "pending", "reason": "maintenance active budget exhausted"},
                )
                continue
            del decisions[decision_count:]
            decision = {
                "accepted": False,
                "status": "round_timeout",
                "reasons": [f"Round deadline ({round_deadline_s}s) exceeded"],
                "base_version": state.adopted.version,
                "candidate": None,
                "round_elapsed_s": round(time.time() - round_started, 1),
            }
            atomic_json(stage / "timeout.json", decision)
            atomic_json(stage / "decision.json", decision)
            decisions.append(decision)
            if wiki is not None:
                await _wiki_record_outbox(
                    wiki,
                    stage,
                    name,
                    "decision",
                    wiki_decision_facts_hook(decision),
                    category="runtime",
                    scope="admission",
                    source=str(stage / "decision.json"),
                    infer=False,
                )
            print(
                json.dumps({"stage": name, "status": "round_timeout"}, ensure_ascii=False),
                flush=True,
            )
        finally:
            if active_budget is not None and active_budget.started is not None:
                active_budget.__exit__(None, None, None)
            ROUND_DEADLINE.reset(deadline_token)
            set_round_deadline(None)
    verify()
    # 汇总训练阶段执行/评测故障：正常评分后的拒绝可完成，评分未完成必须报失败
    unhealthy = stage_health_hook()
    summary = {
        "status": "complete"
        if not unhealthy
        and all(d.get("status") not in ("validation_failed", "round_timeout") for d in decisions)
        else "failed",
        "unhealthy_stages": unhealthy,
        "execution_status": "complete"
        if all(d.get("status") not in ("validation_failed", "round_timeout") for d in decisions)
        else "incomplete",
        "adopted_measurement_status": "complete"
        if state.baseline.completed == state.baseline.total
        and not state.baseline.generation_faults
        and not state.baseline.evaluation_faults
        else "incomplete",
        "adopted_evidence_stage": state.evidence_stage,
        "stopped_by_operator": stopped,
        "rounds": decisions,
        "adopted_version": workspace.branch(branch)["adopted"],
        "working_version": workspace.branch(branch)["working"],
        "pending_wiki_tasks": [
            str(p)
            for p in root.glob("R*/wiki-outbox/*.json")
            if json.loads(p.read_text()).get("state") == "pending"
        ],
        "adopted_scores": state.baseline.to_dict(),
        "stability": stability_metrics(root),
    }
    if control_status is not None:
        summary["status"] = control_status
    if round_deadline_s is not None:
        summary["completed_rounds"] = sum(
            d.get("candidate") is not None
            and d.get("status") not in ("validation_failed", "round_timeout")
            and d["candidate"].get("completed") == d["candidate"].get("total")
            and not d["candidate"].get("generation_faults")
            and not d["candidate"].get("evaluation_faults")
            for d in decisions
        )
    atomic_json(root / "summary.json", summary)
    return summary
