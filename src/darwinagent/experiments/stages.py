"""Formal stage execution, admission orchestration and smoke gating."""

from __future__ import annotations

import inspect
import json
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from darwinagent.contracts import EvaluationResult, RunResult, plain
from darwinagent.engine.pipeline import Pipeline
from darwinagent.kernel.validation import capability_names
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.deadline import bounded_timeout
from darwinagent.runtime.identity import transport_identity

from .recovery import (
    _prior_failed_check_snapshots,
    _prior_failed_tool_params,
    _retry_journal,
    _retryable_answer,
    _settle_reservations,
    answer_fault_category,
    batched_fault_retry,
)
from .spec import aggregate_scores


async def _evaluate_stage(evaluator, result, questions):
    """Subset-aware evaluators opt in; the generic evaluator needs only RunResult."""
    if "asked" in inspect.signature(evaluator.evaluate).parameters:
        return await evaluator.evaluate(result, asked=tuple(q.id for q in questions))
    return await evaluator.evaluate(result)


def _stage_health(*, root):
    """Stage-level execution faults from the on-disk stage records. A candidate rejected
    after complete scoring is a normal outcome; a stage that could not finish scoring is
    a fault and must surface in the run status."""
    health = {}
    for path in sorted(root.glob("*/stage.json")):
        row = json.loads(path.read_text())
        scores = row.get("scores", {})
        faults = {
            "status": row.get("status"),
            "completed": scores.get("completed"),
            "total": scores.get("total"),
            "generation_faults": scores.get("generation_faults"),
            "evaluation_faults": scores.get("evaluation_faults"),
        }
        if (
            row.get("status") != "complete"
            or faults["completed"] != faults["total"]
            or faults["generation_faults"]
            or faults["evaluation_faults"]
        ):
            health[path.parent.name] = faults
    return health


async def _preflight(
    candidate,
    spec,
    sample_question=None,
    cases=None,
    replay_inputs=(),
    *,
    dynamic_trial_graphs_hook,
    rebuild_graph_cached_hook,
    bootstrap_trial_graph,
    config,
    dynamic_trial,
    graph_builder,
    root,
    snapshot_root,
):
    """An identity-bound candidate report, including actual frozen trial inputs."""
    from types import SimpleNamespace

    from .admission import admit_candidate

    if snapshot_root is None and bootstrap_trial_graph is None and not dynamic_trial:
        # Legacy dynamic-graph runs execute their actual-data trials in Pipeline.run.
        return None
    if cases is None:
        cases = (
            SimpleNamespace(
                id="trial", questions=() if sample_question is None else (sample_question,)
            ),
        )
    replay_inputs = tuple(replay_inputs) + tuple(_prior_failed_tool_params(root))
    replay_checks = tuple(
        _prior_failed_check_snapshots(root, getattr(spec, "answer_contract", None))
    )
    report_path = Path(candidate.root).parent / "admission.json"
    required = capability_names(getattr(spec, "retrieval_floor", {}) or {})
    if snapshot_root is not None and graph_builder is None:
        from .admission_worker import run_isolated
        from .snapshots import snapshot_digest

        try:
            snapshot_digests = {c.id: snapshot_digest(snapshot_root / c.id) for c in cases}
        except (OSError, ValueError) as exc:
            from .admission import AdmissionError

            report = {
                "schema_version": 1,
                "candidate_version": candidate.version,
                "asset_fingerprints": {a.id: a.fingerprint for a in candidate.assets.assets},
                "config_digest": digest(config.to_dict()),
                "scenarios": [
                    {
                        "asset_id": "bundle",
                        "scenario_id": "training_graph",
                        "status": "incomplete",
                        "required": True,
                        "error": str(exc),
                    }
                ],
                "verdict": "failed",
            }
            atomic_json(report_path, report)
            raise AdmissionError(report_path, report) from exc
        request = {
            "bundle_path": str(candidate.root),
            "bundle_version": candidate.version,
            "snapshot_root": str(snapshot_root),
            "snapshot_digests": snapshot_digests,
            "asset_fingerprints": {a.id: a.fingerprint for a in candidate.assets.assets},
            "cases": [c.to_dict() for c in cases],
            "config": config.to_dict(),
            "required_caps": sorted(required),
            "report_path": str(report_path),
            "replay_inputs": plain(replay_inputs),
            "replay_checks": plain(replay_checks),
            "answer_counterexamples": plain(getattr(spec, "answer_counterexamples", ()) or ()),
            "answer_examples": plain(getattr(spec, "answer_examples", ()) or ()),
        }
        request_path = report_path.with_name("admission-input.json")
        atomic_json(request_path, request)
        if not run_isolated(request_path, report_path, bounded_timeout(180)):
            from .admission import AdmissionError

            raise AdmissionError(report_path, json.loads(report_path.read_text()))
        return json.loads(report_path.read_text())
    graphs = {}
    if dynamic_trial:
        graphs = await dynamic_trial_graphs_hook(candidate, cases)
    elif graph_builder is not None and snapshot_root is not None:
        # 新模式：准入电池在「按当前 S 重建的图」上跑（与正式答题同一派生规则），
        # 不再用快照冻结图过检——准入面=作答面。逐候选 S 指纹键控缓存。
        # 重建失败（S 与投影词汇不兼容等）落 failed 准入报告并抛
        # AdmissionError——不放宽、不崩溃（B0/候选轮一致）。
        try:
            for case in cases:
                graphs[case.id] = await rebuild_graph_cached_hook(candidate, case)
        except Exception as exc:
            from darwinagent.runtime.steps import AwaitingBudget, RequestAbandoned, UnknownRequest

            if getattr(exc, "continuation_signal", False) or isinstance(
                exc, (UnknownRequest, AwaitingBudget, RequestAbandoned)
            ):
                raise
            from .admission import AdmissionError

            report = {
                "schema_version": 1,
                "candidate_version": candidate.version,
                "asset_fingerprints": {a.id: a.fingerprint for a in candidate.assets.assets},
                "config_digest": digest(config.to_dict()),
                "scenarios": [
                    {
                        "asset_id": "bundle",
                        "scenario_id": "training_graph",
                        "status": "incomplete",
                        "required": True,
                        "error": f"graph rebuild failed: {type(exc).__name__}: {str(exc)[:400]}",
                    }
                ],
                "verdict": "failed",
            }
            atomic_json(report_path, report)
            raise AdmissionError(report_path, report) from exc
    else:
        for case in cases:
            graphs[case.id] = bootstrap_trial_graph
    return admit_candidate(
        candidate,
        cases,
        graphs,
        config,
        required,
        report_path,
        replay_inputs=replay_inputs,
        answer_contract=getattr(spec, "answer_contract", None),
        replay_checks=replay_checks,
        answer_counterexamples=getattr(spec, "answer_counterexamples", ()) or (),
        answer_examples=getattr(spec, "answer_examples", ()) or (),
    )


def _record_smoke(bundle, error, elapsed_s=0):
    path = Path(bundle.root).parent / "admission.json"
    if not path.exists():
        return
    report = json.loads(path.read_text())
    report["smoke"] = {
        "status": "failed" if error else "passed",
        "error": error,
        "elapsed_s": round(elapsed_s, 3),
    }
    if error:
        report["verdict"] = "failed"
    atomic_json(path, report)


async def _stage(
    name,
    cases,
    spec,
    *,
    client_factory,
    config,
    evaluator_factory,
    graph_builder,
    root,
    snapshot_root,
    verify,
):
    """Run every case of the split on the same bundle; aggregate by the frozen sum rule.

    Faulted questions get ONE bounded retry pass: their checkpoints are removed and the
    pipeline reruns (healthy answers checkpoint-reuse at zero cost). A question that
    fails twice is a real fault and stays; the retry is recorded in the stage summary."""
    verify()
    started = time.time()
    stage = root / name
    client = client_factory(name)
    try:
        results = []
        scores = []
        identities = []
        retries = {}
        previous_stage = (
            json.loads((stage / "stage.json").read_text())
            if (stage / "stage.json").exists()
            else {}
        )
        for case in cases:
            pipeline = Pipeline(
                client,
                stage / "generation",
                frozen_snapshot=None if snapshot_root is None else snapshot_root / case.id,
                graph_builder=graph_builder,
            )
            result = await pipeline.run(case, spec, config)
            retried_now = False
            if (
                result.identity in previous_stage.get("run_identities", ())
                and previous_stage.get("asset_version") == spec.bundle.version
                and case.id in previous_stage.get("fault_retries", {})
            ):
                retries[case.id] = previous_stage["fault_retries"][case.id]
            journal_path = stage / "fault-retry" / f"{case.id}.json"
            _settle_reservations(journal_path, result, client.ledger_summary())
            faulted = [a for a in result.answers if a.status == "execution_error"]
            categories = dict(Counter(answer_fault_category(a) for a in faulted))
            graph_failure = [
                d
                for d in (result.graph_diagnostics or ())
                if isinstance(d, Mapping) and d.get("status") == "execution_error"
            ]
            if faulted and graph_failure:
                # 图阶段全局确定性失败（评审①）：删答案检查点救不回图阶段产物，
                # 分批等待重试毫无意义——如实记录，不重试。
                retries[case.id] = {
                    "questions": len(faulted),
                    "recovered": 0,
                    "still_faulted": sorted(a.question_id for a in faulted),
                    "skipped_retry": "graph_stage_failure",
                    "graph_error": str(graph_failure[0].get("error"))[:200],
                }
                print(
                    json.dumps(
                        {"stage": name, "case": case.id, "fault_retry": retries[case.id]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif faulted and not any(_retryable_answer(a) for a in faulted):
                # Contract/check/protocol failures need repair; a rejected review is
                # classified separately and gets one fresh, history-aware retry below.
                retries[case.id] = {
                    "questions": len(faulted),
                    "recovered": 0,
                    "still_faulted": sorted(a.question_id for a in faulted),
                    "skipped_retry": next(iter(categories))
                    if len(categories) == 1
                    else "mixed_unrecoverable_faults",
                    "fault_categories": categories,
                    "sample_errors": [str(a.error)[:150] for a in faulted[:3]],
                }
                print(
                    json.dumps(
                        {"stage": name, "case": case.id, "fault_retry": retries[case.id]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif faulted:
                journal = _retry_journal(journal_path, result.identity)
                eligible = []
                for answer in faulted:
                    if not _retryable_answer(answer):
                        continue
                    previous = journal["questions"].get(answer.question_id)
                    if previous is not None:
                        continue
                    journal["questions"][answer.question_id] = {
                        "state": "reserved",
                        "attempts": 1,
                        "initial_digest": digest(answer.to_dict()),
                        "initial_error_type": str(answer.error).split(":", 1)[0],
                        "fault_category": answer_fault_category(answer),
                        "consumed_before": client.ledger_summary(),
                    }
                    eligible.append(answer)
                    atomic_json(journal_path, journal)
                if not eligible:
                    retries[case.id] = {
                        **retries.get(case.id, {}),
                        "questions": len(faulted),
                        "recovered": 0,
                        "still_faulted": sorted(a.question_id for a in faulted),
                        "skipped_retry": "already_reserved_or_deterministic",
                    }
                else:
                    # 先歇再重试：EmptyCompletion 类故障多为瞬时突发，隔窗后分批小跑；
                    # 统计口径见 batched_fault_retry（末份答案集重算，不做批次并集）。
                    retry_started = time.monotonic()
                    retried_now = True
                    result, still_faulted = await batched_fault_retry(
                        pipeline,
                        case,
                        spec,
                        config,
                        stage / "generation" / case.id / "answers",
                        eligible,
                        **(
                            {"lead_s": 0, "gap_s": 0}
                            if all(answer_fault_category(a) == "review_exhausted" for a in eligible)
                            else {}
                        ),
                    )
                    latest = {a.question_id: a for a in result.answers}
                    for answer in eligible:
                        journal["questions"][answer.question_id].update(
                            state="done",
                            consumed_after=client.ledger_summary(),
                            final_error_type=str(latest[answer.question_id].error).split(":", 1)[0]
                            if latest[answer.question_id].status == "execution_error"
                            else None,
                        )
                    atomic_json(journal_path, journal)
                    retries[case.id] = {
                        "questions": len(faulted),
                        "retried": len(eligible),
                        "recovered": sum(a.question_id not in still_faulted for a in eligible),
                        "still_faulted": still_faulted,
                        "elapsed_s": round(time.monotonic() - retry_started, 3),
                        "skipped_deterministic": len(faulted) - len(eligible),
                    }
                retries[case.id]["fault_categories"] = categories
                print(
                    json.dumps(
                        {"stage": name, "case": case.id, "fault_retry": retries[case.id]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            scores_path = stage / "evaluation" / f"{case.id}.json"
            if retried_now:
                scores_path.unlink(
                    missing_ok=True
                )  # 重试跑过＝答案集可能已变：旧评测检查点一律作废重评
            if scores_path.exists():
                saved = json.loads(scores_path.read_text())
                if (
                    saved["run_identity"] != result.identity
                    or saved["asset_version"] != spec.bundle.version
                ):
                    raise ValueError("Evaluation checkpoint identity mismatch")
                case_scores = EvaluationResult(**saved["scores"])
            else:
                evaluator = evaluator_factory(client, stage / "evaluation" / case.id)
                # 完整性按本轮实际出题集核对（训练集瘦身后是前缀子集；全量时等价旧检查）
                case_scores = await _evaluate_stage(evaluator, result, case.questions)
                atomic_json(
                    scores_path,
                    {
                        "run_identity": result.identity,
                        "asset_version": spec.bundle.version,
                        "scores": case_scores.to_dict(),
                    },
                )
            results.append(result)
            scores.append(case_scores)
            identities.append(result.identity)
        aggregated = aggregate_scores(scores)
        verify()
        summary = {
            "stage": name,
            "cases": [c.id for c in cases],
            "status": "complete"
            if aggregated.completed == aggregated.total
            and not aggregated.generation_faults
            and not aggregated.evaluation_faults
            else "failed",
            "run_identities": identities,
            "asset_version": spec.bundle.version,
            "scores": aggregated.to_dict(),
            "fault_retries": retries,
            "calls": client.ledger_summary(),
            "elapsed_s": round(time.time() - started, 2),
        }
        atomic_json(stage / "stage.json", summary)
        print(
            json.dumps(
                {k: summary[k] for k in ("stage", "status", "asset_version", "elapsed_s")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return results, aggregated
    finally:
        await client.aclose()


async def _smoke_gate(
    cases,
    spec,
    questions_per_case=6,
    candidate=False,
    *,
    client_factory,
    config,
    graph_builder,
    smoke_judge,
    snapshot_root,
    root=None,
    execution=None,
):
    """B0 全量提交前的冒烟门（用户拍板：先保证能答对，再启动跑；v10 追加：6 题对 2）：
    每训练对话抽前 6 题走完整真实管线＋冻结判题（真模型、~5 分钟）。
    显式非 strict 执行持久保存每版冒烟步骤和回执；旧模式仍用临时目录。
    过门条件＝执行错误 <2/3、判题完整、至少 2/6 precise 答对；不满足分钟级中止换根
    ——确定性全灭（v1/v3/v6 事故类）与「能跑但全答错」的弱冷启动都不再烧全量预算。
    门槛与 B0 冷门成比例：单题低概率故障（如 F 输出契约被个别调用绊倒）由冷门吸收，
    只有系统性破绽（≥2/3）才在此拦下。"""
    import dataclasses as _dc
    import tempfile
    from contextlib import nullcontext

    durable = execution is not None and not execution.strict
    if durable and root is None:
        raise ValueError("Explicit daily smoke requires a persistent experiment root")
    if durable:
        from darwinagent.runtime.workspace import Workspace

        smoke_root = Path(root) / "smoke" / spec.bundle.version
        workspace = Workspace(Path(root) / "workspace")
        atomic_json(
            smoke_root / "identity.json",
            {
                "candidate_version": spec.bundle.version,
                "config": config.to_dict(),
                "execution": execution.to_dict(),
            },
        )

    client = client_factory("B0-smoke")
    try:
        for case in cases:
            if durable and not execution.includes(case.id):
                continue
            questions = tuple(
                q for q in case.questions if not durable or execution.includes(case.id, q.id)
            )
            if not questions:
                continue
            if candidate:
                risk = (
                    "时间|日期|哪天|何时|上周|昨天",
                    "过滤|全部|哪些|多少|类型|主题",
                    "关系|相关|属于|关联|遍历",
                )
                import re

                selected = []
                for pattern in risk:
                    first = next(
                        (q for q in questions if q not in selected and re.search(pattern, q.text)),
                        None,
                    )
                    if first is not None:
                        selected.append(first)
                selected.extend(q for q in questions if q not in selected)
                questions = tuple(selected)
            sampled = _dc.replace(case, questions=tuple(questions[:questions_per_case]))
            with nullcontext(smoke_root) if durable else tempfile.TemporaryDirectory() as td:
                pipeline = Pipeline(
                    client,
                    Path(td),
                    frozen_snapshot=None if snapshot_root is None else snapshot_root / case.id,
                    graph_builder=graph_builder,
                    **(
                        {
                            "workspace": workspace,
                            "preparation_root": Path(root) / "shared-preparation",
                        }
                        if durable
                        else {}
                    ),
                )
                result = await pipeline.run(
                    sampled, spec, config, **({"execution": execution} if durable else {})
                )
                faults = [a for a in result.answers if a.status == "execution_error"]
                recoverable = [a for a in faults if _retryable_answer(a)]
                # Daily retries are selected explicitly by ExecutionSelection; the legacy
                # helper clears legacy checkpoints and omits journals, so never use it here.
                if not durable and candidate and recoverable and len(recoverable) == len(faults):
                    result, _ = await batched_fault_retry(
                        pipeline,
                        sampled,
                        spec,
                        config,
                        Path(td) / case.id / "answers",
                        recoverable,
                        **(
                            {"lead_s": 0, "gap_s": 0}
                            if all(
                                answer_fault_category(a) == "review_exhausted" for a in recoverable
                            )
                            else {}
                        ),
                    )
            faults = [a for a in result.answers if a.status == "execution_error"]
            if candidate and faults:
                return (
                    f"候选冒烟执行故障 {len(faults)}/{len(result.answers)}: "
                    + str(faults[0].error)[:200]
                )
            if len(faults) * 3 >= len(result.answers) * 2:
                return (
                    f"冒烟执行错误达 {len(faults)}/{len(result.answers)}（≥2/3，系统性破绽）: "
                    + str(faults[0].error)[:200]
                )
            if not any(a.status in ("answered", "abstained") for a in result.answers):
                return "冒烟题无任何有效作答"
            # 冒烟判题（任务层注入的冻结判题原语，训练集金标对机械门合法可见）：
            # 保证能答对，至少 1/3 precise。无注入时退化为「存在有效作答」检查。
            if smoke_judge is not None:
                verdict = await smoke_judge(client, case, result.answers)
                if verdict["completed"] < verdict["total"]:
                    return f"冒烟判题未完成: {verdict}"
                if verdict["precise"] < 2:
                    return f"冒烟 {verdict['total']} 题对 {verdict['precise']}（需≥2）——质量门拒绝"
            elif not any(a.status in ("answered", "abstained") for a in result.answers):
                return "冒烟题无任何有效作答"
        return None
    finally:
        await client.aclose()


async def selected_stage(
    name,
    cases,
    spec,
    *,
    root,
    client_factory,
    config,
    evaluator_factory,
    execution,
    snapshot_root=None,
    graph_builder=None,
):
    """Execute exactly the selected stages. Scoring never implies answering."""
    from darwinagent.runtime.artifacts import digest
    from darwinagent.runtime.workspace import Workspace

    workspace = Workspace(Path(root) / "workspace")
    stage = Path(root) / name
    client = client_factory(name)
    results, scores = [], []
    all_comparable = True
    criteria = []
    try:
        for case in cases:
            if not execution.includes(case.id):
                continue
            selected = tuple(q for q in case.questions if execution.includes(case.id, q.id))
            if set(execution.stages) & {"facts", "graph", "retrieval", "answer", "check", "review"}:
                result = await Pipeline(
                    client,
                    stage / "generation",
                    frozen_snapshot=None if snapshot_root is None else snapshot_root / case.id,
                    graph_builder=graph_builder,
                    workspace=workspace,
                    preparation_root=Path(root) / "shared-preparation",
                ).run(case, spec, config, execution=execution)
            else:
                # Existing answers carry their actual graph, question and producer provenance.
                try:
                    branch_record = workspace.branch(execution.branch)
                except KeyError:
                    branch_record = None
                if branch_record and branch_record.get("parent"):
                    from darwinagent.runtime.continuation import fork_case_progress

                    fork_case_progress(
                        stage / "generation" / case.id,
                        branch_record["parent"],
                        execution.branch,
                        selected,
                        workspace=workspace,
                    )
                answers = []
                saved_sources = []
                for q in selected:
                    path = (
                        stage
                        / "generation"
                        / case.id
                        / "branches"
                        / execution.branch
                        / "answers"
                        / (digest(q.to_dict()) + ".json")
                    )
                    if not path.exists():
                        legacy = (
                            stage / "generation" / case.id / "answers" / (digest(q.id) + ".json")
                        )
                        if not legacy.exists():
                            raise ValueError(
                                f"Missing saved answer for {case.id}/{q.id}; answer stage is not selected"
                            )
                        from darwinagent.runtime.continuation import register_legacy_case

                        report = register_legacy_case(
                            stage / "generation" / case.id,
                            case,
                            branch=execution.branch,
                            workspace=workspace,
                        )
                        if not path.exists():
                            raise ValueError(
                                f"Question version cannot be established for {case.id}/{q.id}: {report['not_registered']}"
                            )
                    stored = json.loads(path.read_text())
                    if stored.get("question_version") != digest(q.to_dict()):
                        raise ValueError("Saved answer belongs to a different question version")
                    if digest(stored["result"]) != stored["digest"]:
                        raise ValueError("Saved answer checksum mismatch")
                    from darwinagent.contracts import AnswerResult

                    answers.append(AnswerResult.from_dict(stored["result"]))
                    from darwinagent.engine.pipeline import answer_source_record

                    saved_sources.append(answer_source_record(stored, q.id, workspace))
                original = stage / "generation" / case.id / "result.json"
                if original.exists():
                    original_result = json.loads(original.read_text())
                    original_result["answers"] = tuple(answers)
                    original_result["answer_provenance"] = tuple(saved_sources)
                    result = RunResult(**original_result)
                else:
                    result = RunResult(
                        case.id,
                        digest([a.to_dict() for a in answers]),
                        spec.bundle.version,
                        tuple(answers),
                        0,
                        (),
                        answer_provenance=tuple(saved_sources),
                    )
            answer_ref = workspace.put_json(result.to_dict())
            workspace.add_provenance(
                answer_ref,
                {
                    "stage": name,
                    "selection": execution.to_dict(),
                    "producer_identity": result.identity,
                },
            )
            results.append(result)
            if "score" in execution.stages:
                criterion = getattr(evaluator_factory, "criterion_id", None)
                if callable(criterion):
                    criterion = criterion()
                reliable_criterion = criterion is not None
                criterion = (
                    criterion if reliable_criterion else "unknown:explicit-rescoring-required"
                )
                question_map = {q.id: q for q in selected}
                per_question = []
                records = []
                comparison_reliable = reliable_criterion
                legacy_navigation = stage / "evaluation" / (case.id + ".json")
                legacy_score = (
                    json.loads(legacy_navigation.read_text())
                    if legacy_navigation.exists()
                    else None
                )
                if (
                    execution.mode == "continue"
                    and legacy_score is not None
                    and not legacy_score.get("question_scores")
                ):
                    versions = [
                        stage
                        / "generation"
                        / case.id
                        / "branches"
                        / execution.branch
                        / "answers"
                        / (digest(q.to_dict()) + ".json")
                        for q in selected
                    ]
                    producer_ids = [
                        json.loads(p.read_text())["identity"] for p in versions if p.exists()
                    ]
                    if (
                        len(producer_ids) == len(result.answers)
                        and len(result.answers) == legacy_score["scores"]["total"]
                        and all(i == legacy_score["run_identity"] for i in producer_ids)
                    ):
                        scores.append(EvaluationResult(**legacy_score["scores"]))
                        comparison_reliable = False
                        all_comparable = False
                        criteria.append("legacy:unknown")
                        continue
                for answer in result.answers:
                    q = question_map[answer.question_id]
                    binding = {
                        "answer": answer.to_dict(),
                        "question": q.to_dict(),
                        "case_id": case.id,
                    }
                    binding_id = digest(binding)
                    key = digest({**binding, "criterion": criterion})
                    checkpoint = stage / "evaluation" / "by-answer" / (key + ".json")
                    navigation = stage / "evaluation" / "questions" / (binding_id + ".json")
                    previous = json.loads(navigation.read_text()) if navigation.exists() else None
                    saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
                    reuse_score = (
                        saved is not None and reliable_criterion and execution.mode == "continue"
                    )
                    if execution.mode == "continue" and saved is None and previous is not None:
                        # Changed criterion does not silently launch a new baseline judging pass.
                        saved, reuse_score = previous, True
                        comparison_reliable = False
                    if execution.mode == "retry_failed":
                        if previous is None:
                            continue
                        saved = previous
                        reuse_score = not previous["scores"].get("evaluation_faults")
                    if reuse_score:
                        score = EvaluationResult(**saved["scores"])
                        actual_criterion = saved["criterion"]
                    else:
                        from uuid import uuid4

                        from darwinagent.runtime.journal_client import JournalClient
                        from darwinagent.runtime.steps import StepJournal

                        attempts_root = stage / "evaluation" / "attempts" / key
                        if execution.mode in ("rerun", "rerun_all", "retry_failed"):
                            attempts_root = attempts_root / uuid4().hex
                        scoring_client = JournalClient(
                            client,
                            StepJournal(
                                attempts_root / "requests",
                                workspace=workspace,
                                bypass_cache=execution.mode
                                in ("rerun", "rerun_all", "retry_failed"),
                                provenance={
                                    "stage": "score",
                                    "criterion": criterion,
                                    "answer_fingerprint": digest(answer.to_dict()),
                                    "question_version": digest(q.to_dict()),
                                },
                            ),
                        )
                        evaluator = evaluator_factory(scoring_client, attempts_root)
                        from dataclasses import replace

                        single = replace(result, answers=(answer,))
                        score = await _evaluate_stage(evaluator, single, (q,))
                        if scoring_client.pending_error is not None:
                            raise scoring_client.pending_error
                        score_ref = workspace.put_json(score.to_dict())
                        workspace.add_reference(
                            score_ref, "actual_answer", workspace.put_json(answer.to_dict())
                        )
                        workspace.add_reference(
                            score_ref, "question_version", workspace.put_json(q.to_dict())
                        )
                        workspace.add_provenance(
                            score_ref,
                            {
                                "criterion": criterion,
                                "stage": name,
                                "producer": transport_identity(client),
                            },
                        )
                        saved = {
                            "scores": score.to_dict(),
                            "answer_ref": answer_ref,
                            "criterion": criterion,
                            "question_version": digest(q.to_dict()),
                            "answer_fingerprint": digest(answer.to_dict()),
                            "score_ref": score_ref,
                        }
                        atomic_json(checkpoint, saved)
                        atomic_json(navigation, saved)
                        actual_criterion = criterion
                    per_question.append(score)
                    records.append(
                        {
                            "question_id": q.id,
                            "criterion": actual_criterion,
                            "checkpoint": str(checkpoint),
                            "reused": reuse_score,
                        }
                    )
                if not per_question:
                    continue
                score = aggregate_scores(per_question)
                navigation = stage / "evaluation" / (case.id + ".json")
                if navigation.exists():
                    workspace.put_bytes(navigation.read_bytes(), format="legacy-score-navigation")
                atomic_json(
                    navigation,
                    {
                        "run_identity": result.identity,
                        "asset_version": spec.bundle.version,
                        "scores": score.to_dict(),
                        "answer_ref": answer_ref,
                        "criterion": criterion,
                        "question_scores": records,
                        "comparison_reliable": comparison_reliable,
                    },
                )
                scores.append(score)
                all_comparable = all_comparable and comparison_reliable
                criteria.append(criterion)
        aggregate = aggregate_scores(scores) if scores else None
        atomic_json(
            stage / "stage.json",
            {
                "stage": name,
                "cases": [c.id for c in cases],
                "status": "complete"
                if aggregate is None or aggregate.completed == aggregate.total
                else "partial",
                "asset_version": spec.bundle.version,
                "run_identities": [r.identity for r in results],
                "scores": None if aggregate is None else aggregate.to_dict(),
                "selection": execution.to_dict(),
                "calls": client.ledger_summary() if hasattr(client, "ledger_summary") else {},
                "elapsed_s": 0,
                "criterion": sorted(set(criteria)),
                "comparison_reliable": all_comparable,
            },
        )
        return results, aggregate
    finally:
        await client.aclose()
