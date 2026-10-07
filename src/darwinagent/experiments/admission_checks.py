"""Independent check batteries and historical check replay trials."""

from __future__ import annotations

from darwinagent.contracts import plain
from darwinagent.kernel.checks import (
    counterexample_snapshot,
    synthetic_answer_battery,
    synthetic_invalid_answer_snapshot,
)
from darwinagent.operators.data import DataCapabilities
from darwinagent.runtime.artifacts import digest

from .admission_reporting import TrialBatch, _row


def check_trials(case, graph, checks, answer_contract, answer_examples, answer_counterexamples):
    scenarios = []
    graph_rows = list(DataCapabilities(graph).rows.values())
    snapshots = [("graph", "must_pass", {"stage": "graph", "nodes": graph_rows})]
    if case.questions:
        q = case.questions[0]
        snapshots += [
            (f"answer_{i}", expectation, v)
            for i, (expectation, v) in enumerate(
                synthetic_answer_battery(
                    graph_rows, q.text, plain(q.parameters), answer_contract=answer_contract
                )
            )
        ]
        snapshots.append(
            (
                "answer_invalid",
                "must_reject",
                synthetic_invalid_answer_snapshot(q.text, plain(q.parameters)),
            )
        )
        # 任务层正常 answered 正例（三次复查 P1）：自洽可验证夹具（自带问题/
        # 参数），真实合法候选必须被接受——「只接受弃答、拒绝所有 answered」
        # 或冻结容器误判的 C 在此被拦；换 case/图不会把旧行程错装进来。
        for ex in answer_examples:
            snapshots.append(
                (
                    f"answer_ex_{ex.get('name', 'x')}",
                    "must_pass",
                    counterexample_snapshot(ex, graph_rows, q.text, plain(q.parameters)),
                )
            )
        for cx in answer_counterexamples:
            snapshots.append(
                (
                    f"answer_cx_{cx.get('name', 'x')}",
                    "must_reject",
                    counterexample_snapshot(cx, graph_rows, q.text, plain(q.parameters)),
                )
            )
    for tag, expectation, snapshot in snapshots:
        checked = checks.trial_report(snapshot["stage"], snapshot)
        invalid_rejected = any(
            item["status"] == "failed" and item.get("ok") is False for item in checked
        )
        for item in checked:
            asset = checks.checks[item["check_id"]][0]
            if expectation == "must_reject":
                passed = invalid_rejected and (
                    item["status"] == "passed" or item.get("ok") is False
                )
            elif expectation == "structure":
                # 结构行（契约占位实例）：冻结容器下可执行＋意见良构即可；
                # 语义拒绝合法——占位实例不要求 C 认可（2026-10-05 审查 P1）。
                passed = not item.get("error_type")
            else:
                passed = item["status"] == "passed"
            scenarios.append(
                _row(
                    asset,
                    tag,
                    "passed" if passed else "failed",
                    ref=f"{case.id}:{tag}",
                    expectation=expectation,
                    **{
                        k: v
                        for k, v in item.items()
                        if k not in ("check_id", "fingerprint", "stage", "status")
                    },
                )
            )
    yield TrialBatch(scenarios)


def check_replay_trials(case, checks, replay_checks):
    scenarios = []
    for row in replay_checks:
        if row.get("case_id") != case.id:
            continue
        snapshot = row.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        snapshot_digest = row.get("snapshot_digest") or digest(snapshot)
        ref = f"{case.id}:{row.get('question_id')}:{str(snapshot_digest)[:12]}"
        expectation = row.get("expectation", "informational")
        registered = {aid for aid, (a, _) in checks.checks.items() if a.stage == "answer"}
        bound = [aid for aid in row.get("check_ids", ()) if aid in registered]
        opinions = checks.trial_report("answer", snapshot)
        rejected = any(item.get("ok") is False for item in opinions)
        if expectation == "must_reject":
            # 畸形历史输入：任何候选的答案阶段 C 至少一个必须拒（装饰性 C 守门）。
            scenarios.append(
                _row(
                    None,
                    "check_replay_reject",
                    "skipped" if not registered else ("passed" if rejected else "failed"),
                    required=bool(registered),
                    ref=ref,
                    expectation=expectation,
                    reason=row.get("reason", ""),
                    check_ids=sorted(bound) or sorted(registered),
                    source=str(row.get("source", "")),
                )
            )
        elif expectation == "verified_must_pass":
            # 具体复现验证过的合法快照：绑定的历史检查资产在候选中必须通过——
            # 同一资产过准入不等于历史问题修复，修复必须重放不再复现失败签名。
            if not bound:
                scenarios.append(
                    _row(
                        None,
                        "check_replay_verified",
                        "incomplete",
                        ref=ref,
                        expectation=expectation,
                        required=True,
                        error="Historical check assets not registered: "
                        + str(sorted(row.get("check_ids", ()))),
                        source=str(row.get("source", "")),
                    )
                )
            else:
                for item in opinions:
                    if item["check_id"] not in bound:
                        continue
                    asset = checks.checks[item["check_id"]][0]
                    scenarios.append(
                        _row(
                            asset,
                            "check_replay_verified",
                            "passed" if item["status"] == "passed" else "failed",
                            ref=ref,
                            expectation=expectation,
                            verified_by=row.get("reason", ""),
                            **{
                                k: v
                                for k, v in item.items()
                                if k not in ("check_id", "fingerprint", "stage", "status")
                            },
                        )
                    )
        else:
            # 类型合法但被拒：只记录回放结果，不做 pass/fail（合法语义拒绝不自动判
            # bug；归因假设不得覆盖这里的检查事实）。
            for item in opinions:
                if bound and item["check_id"] not in bound:
                    continue
                scenarios.append(
                    _row(
                        checks.checks[item["check_id"]][0],
                        "check_replay_info",
                        "passed" if item["status"] == "passed" else "failed",
                        required=False,
                        ref=ref,
                        expectation=expectation,
                        historical_issues=row.get("issues", ()),
                        **{
                            k: v
                            for k, v in item.items()
                            if k not in ("check_id", "fingerprint", "stage", "status")
                        },
                    )
                )
        yield TrialBatch(scenarios)
    yield TrialBatch(scenarios, persist=False)
