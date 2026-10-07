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
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.deadline import ROUND_DEADLINE

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
):
    # The stage whose evaluation currently backs `baseline`/`results`: proposals must
    # read diagnostics from THERE, never from the round being proposed (it has not
    # run yet). A rejected candidate leaves it unchanged.
    state = _IterationState(adopted, baseline, results, "B0", val_baseline)
    decisions = []
    stopped = False
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
            if validation_plan is not None and decision.get("validation"):
                val_history[name] = decision["validation"]
                atomic_json(root / "validation.json", val_history)
            if wiki is not None:
                if decision.get("candidate") is not None:
                    await wiki_formal_from_disk_hook(wiki, name, cases, decision["candidate"])
                formal_entry = next(
                    (
                        e
                        for e in reversed(wiki._wiki()["entries"])
                        if e["stage"] == name and e["kind"] == "formal"
                    ),
                    None,
                )
                await wiki.record(
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
                revisions.publish(restored, root / "published", decision)
                if stage_gate is not None:
                    stage_gate(name)
                restored_results, _ = await stage_hook(name, cases, spec.with_bundle(restored))
                state = _IterationState(
                    restored, restored_scores, restored_results, name, restored_validation
                )
            continue
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
        if round_deadline_s is not None:
            if budget_path.exists():
                budget = json.loads(budget_path.read_text())
                if budget["limit_s"] != round_deadline_s:
                    raise ValueError("Resumed round budget changed")
            else:
                budget = {
                    "limit_s": round_deadline_s,
                    "started_at": round_started,
                    "deadline_at": round_started + round_deadline_s,
                }
                atomic_json(budget_path, budget)
            round_started = budget["started_at"]
            remaining = min(round_deadline_s, budget["deadline_at"] - time.time())
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
                                wiki, stage, name, cases, spec, state.adopted, state.results, scope
                            )
                        except WikiAdmissionExhausted as exc:
                            decision = {
                                "accepted": False,
                                "status": "validation_failed",
                                "reasons": [f"{type(exc).__name__}: {exc}"],
                                "base_version": state.adopted.version,
                                "candidate": None,
                            }
                            atomic_json(decision_path, decision)
                            decisions.append(decision)
                            await wiki.record(
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
                            state.adopted,
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
                decision = {
                    **selection_policy.decide(state.baseline, candidate_scores),
                    "base_version": state.adopted.version,
                    "candidate_version": candidate.version,
                }
                # 新模式：P.extract 此模式不执行——其补丁不得报告为已生效优化。
                if graph_builder is not None:
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
                # 保持旧循环的落盘顺序：Wiki 中断后可从正式决策补写经验。
                # 有整轮预算时推迟决策落盘，防 Wiki 超时留下可采纳记录。
                if round_deadline_s is None:
                    atomic_json(decision_path, decision)
                    decisions.append(decision)
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
                        **asset_evidence(state.adopted, candidate),
                        **_wiki_training_evidence(
                            cases,
                            candidate_results,
                            state.results,
                            _per_case_feedback_facts(root, name, cases),
                        ),
                    }
                    if decision.get("validation"):
                        formal_facts["validation"] = decision["validation"]  # 聚合指标
                    if decision.get("p_extract_not_effective"):
                        formal_facts["p_extract_not_effective"] = decision[
                            "p_extract_not_effective"
                        ]
                    await wiki.record(
                        name,
                        "formal",
                        formal_facts,
                        category="strategy",
                        scope="formal",
                        training_ids=[tid for case in cases for tid in question_identity(case)],
                        source=str(stage / "stage.json"),
                    )
                    await wiki.record(
                        name,
                        "decision",
                        {**wiki_decision_facts_hook(decision), **formal_facts},
                        training_ids=[tid for case in cases for tid in question_identity(case)],
                        category="strategy",
                        scope="formal",
                        source=str(decision_path),
                        infer=True,
                    )
                if get_round_deadline() is not None and time.monotonic() >= get_round_deadline():
                    raise TimeoutError("Round deadline exceeded before publication")
                decision["round_elapsed_s"] = round(time.time() - round_started, 1)
                if round_deadline_s is not None:
                    atomic_json(decision_path, decision)
                    decisions.append(decision)
                if decision["accepted"]:
                    published = revisions.publish(candidate, root / "published", decision)
                    state = _IterationState(
                        published, candidate_scores, candidate_results, name, candidate_validation
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
        except TimeoutError:
            if get_round_deadline() is None or time.monotonic() < get_round_deadline():
                raise
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
                await wiki.record(
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
        "stopped_by_operator": stopped,
        "rounds": decisions,
        "adopted_version": state.adopted.version,
        "adopted_scores": state.baseline.to_dict(),
        "stability": stability_metrics(root),
    }
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
