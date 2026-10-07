"""Formal stage execution, admission orchestration and smoke gating."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Mapping
from pathlib import Path

from darwinagent.contracts import EvaluationResult, plain
from darwinagent.engine.pipeline import Pipeline
from darwinagent.kernel.validation import capability_names
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.deadline import bounded_timeout

from .recovery import (
    _prior_failed_check_snapshots,
    _prior_failed_tool_params,
    _retry_journal,
    _retryable_answer,
    _settle_reservations,
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
                graphs[case.id] = rebuild_graph_cached_hook(candidate, case)
        except Exception as exc:
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
        for case in cases:
            pipeline = Pipeline(
                client,
                stage / "generation",
                frozen_snapshot=None if snapshot_root is None else snapshot_root / case.id,
                graph_builder=graph_builder,
            )
            result = await pipeline.run(case, spec, config)
            journal_path = stage / "fault-retry" / f"{case.id}.json"
            _settle_reservations(journal_path, result, client.ledger_summary())
            faulted = [a for a in result.answers if a.status == "execution_error"]
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
                # 确定性工具错误（评审：接口/参数错误重试不会变好）：不整题重检索，
                # 如实入统计与反馈，由资产修订解决（scope F）。
                retries[case.id] = {
                    "questions": len(faulted),
                    "recovered": 0,
                    "still_faulted": sorted(a.question_id for a in faulted),
                    "skipped_retry": "deterministic_tool_error",
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
                        "consumed_before": client.ledger_summary(),
                    }
                    eligible.append(answer)
                    atomic_json(journal_path, journal)
                if not eligible:
                    retries[case.id] = {
                        "questions": len(faulted),
                        "recovered": 0,
                        "still_faulted": sorted(a.question_id for a in faulted),
                        "skipped_retry": "already_reserved_or_deterministic",
                    }
                else:
                    # 先歇再重试：EmptyCompletion 类故障多为瞬时突发，隔窗后分批小跑；
                    # 统计口径见 batched_fault_retry（末份答案集重算，不做批次并集）。
                    retry_started = time.monotonic()
                    result, still_faulted = await batched_fault_retry(
                        pipeline,
                        case,
                        spec,
                        config,
                        stage / "generation" / case.id / "answers",
                        eligible,
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
                print(
                    json.dumps(
                        {"stage": name, "case": case.id, "fault_retry": retries[case.id]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            scores_path = stage / "evaluation" / f"{case.id}.json"
            if case.id in retries:
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
            if aggregated.completed == aggregated.total and not aggregated.evaluation_faults
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
):
    """B0 全量提交前的冒烟门（用户拍板：先保证能答对，再启动跑；v10 追加：6 题对 2）：
    每训练对话抽前 6 题走完整真实管线＋冻结判题（临时目录、真模型、~5 分钟）。
    过门条件＝执行错误 <2/3、判题完整、至少 2/6 precise 答对；不满足分钟级中止换根
    ——确定性全灭（v1/v3/v6 事故类）与「能跑但全答错」的弱冷启动都不再烧全量预算。
    门槛与 B0 冷门成比例：单题低概率故障（如 F 输出契约被个别调用绊倒）由冷门吸收，
    只有系统性破绽（≥2/3）才在此拦下。"""
    import dataclasses as _dc
    import tempfile

    client = client_factory("B0-smoke")
    try:
        for case in cases:
            questions = case.questions
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
            with tempfile.TemporaryDirectory() as td:
                pipeline = Pipeline(
                    client,
                    Path(td),
                    frozen_snapshot=None if snapshot_root is None else snapshot_root / case.id,
                    graph_builder=graph_builder,
                )
                result = await pipeline.run(sampled, spec, config)
                faults = [a for a in result.answers if a.status == "execution_error"]
                recoverable = [a for a in faults if _retryable_answer(a)]
                if candidate and recoverable and len(recoverable) == len(faults):
                    result, _ = await batched_fault_retry(
                        pipeline,
                        sampled,
                        spec,
                        config,
                        Path(td) / case.id / "answers",
                        recoverable,
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
