"""Contract-compatible function chains and function/check compositions."""

from __future__ import annotations

from darwinagent.contracts import plain
from darwinagent.kernel.checks import synthetic_answer_snapshot
from darwinagent.kernel.spec import validate_value
from darwinagent.runtime.artifacts import digest

from .admission_reporting import TrialBatch, _row


def composition_trials(case, graph, registry, checks, composable, answer_contract):
    scenarios = []
    combinations = 0
    for source_id, (source, rows) in composable.items():
        for target_id, (target, _) in sorted(registry.functions.items()):
            for base in target.trial_inputs[:1]:
                params = {**plain(base), "rows": rows}
                if "rows" not in base:
                    continue
                try:
                    validate_value(params, target.input_contract, "tool.params")
                except ValueError:
                    continue
                combinations += 1
                ref = f"{case.id}:{source_id}->{target_id}:{digest(params)}"
                observation = {}
                try:
                    registry.call(target_id, params, graph, _observation=observation)
                    status, error_type, error = "passed", None, None
                except Exception as exc:
                    status, error_type, error = "failed", type(exc).__name__, str(exc)
                scenarios.append(
                    _row(
                        target,
                        "function_chain",
                        status,
                        ref=ref,
                        source_asset_id=source_id,
                        source_fingerprint=source["asset_fingerprint"],
                        error_type=error_type,
                        error=error,
                        **observation,
                    )
                )
        if case.questions and any(a.stage == "answer" for a, _ in checks.checks.values()):
            q = case.questions[0]
            evidence = [r for r in rows if r.get("node_id") in source["node_ids"]]
            if evidence:
                snapshot = synthetic_answer_snapshot(
                    evidence, q.text, plain(q.parameters), answer_contract=answer_contract
                )
                # 契约占位实例（非 string 根类型）在 function_check 同样只做
                # 结构判定：可执行＋意见良构；语义拒绝合法（2026-10-05 审查 P1）。
                from collections.abc import Mapping as _Map

                _root = (
                    (answer_contract or {}).get("type")
                    if isinstance(answer_contract, _Map)
                    else None
                )
                _structure = _root not in (None, "string", "any")
                for item in checks.trial_report("answer", snapshot):
                    asset = checks.checks[item["check_id"]][0]
                    combinations += 1
                    passed = (
                        not item.get("error_type") if _structure else item["status"] == "passed"
                    )
                    scenarios.append(
                        _row(
                            asset,
                            "function_check",
                            "passed" if passed else "failed",
                            ref=f"{case.id}:{source_id}->{asset.id}:{digest(snapshot)}",
                            source_asset_id=source_id,
                            **{
                                k: v
                                for k, v in item.items()
                                if k not in ("check_id", "fingerprint", "stage", "status")
                            },
                        )
                    )
    if not combinations:
        scenarios.append(
            _row(
                None,
                "composition",
                "skipped",
                required=False,
                ref=case.id,
                reason="No contract-compatible row output and consumer",
            )
        )
    yield TrialBatch(scenarios)
