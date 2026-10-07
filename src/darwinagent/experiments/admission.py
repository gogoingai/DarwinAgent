"""Fail-closed, per-candidate actual-graph admission diagnostics."""

from __future__ import annotations

import ast
import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from darwinagent.contracts import plain
from darwinagent.kernel.checks import (
    CheckRegistry,
    counterexample_snapshot,
    synthetic_answer_battery,
    synthetic_answer_snapshot,
    synthetic_invalid_answer_snapshot,
)
from darwinagent.kernel.functions import FunctionRegistry
from darwinagent.kernel.spec import validate_value
from darwinagent.kernel.validation import trial_capability_floor_errors, validate_bundle
from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import Limits
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import snapshot_files
from .admission_rules import loop_carried_capability_errors


class AdmissionError(ValueError):
    def __init__(self, report_path, report):
        failures = [
            f"{x['asset_id']}:{x['scenario_id']}:{x.get('error', x['status'])}"
            for x in report["scenarios"]
            if x["required"] and x["status"] != "passed"
        ]
        prefix = (
            "候选能力试跑不合格: "
            if any(
                x["scenario_id"] == "capability_floor" and x["status"] == "failed"
                for x in report["scenarios"]
            )
            else "候选准入失败: "
        )
        super().__init__(prefix + f"({report_path}): " + "; ".join(failures[:12]))
        self.report_path, self.report = report_path, report


def _row(asset, scenario, status, *, ref="", required=True, **extra):
    return {
        "asset_id": asset.id if asset else "bundle",
        "asset_fingerprint": asset.fingerprint if asset else None,
        "scenario_id": scenario,
        "input_ref": ref,
        "required": required,
        "status": status,
        **extra,
    }


def _graph_identity(graph):
    # GraphResult is not JSON; the sorted data-bearing rows provide a stable identity.
    return digest(sorted(DataCapabilities(graph).rows.values(), key=lambda r: r["node_id"]))


def _returned_rows(data):
    rows = data.get("rows") if isinstance(data, Mapping) else data
    if isinstance(rows, (list, tuple)) and rows and all(isinstance(x, Mapping) for x in rows):
        return plain(rows)
    return ()


def _traversal_relations(asset, params):
    """Resolve literal/parameter relation selectors without executing candidate code."""
    tree = ast.parse(asset.content)
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)

    def resolve(selector, seen=()):
        if isinstance(selector, ast.Name):
            values = bindings.get(selector.id, ())
            if len(values) == 1 and selector.id not in seen:
                return resolve(values[0], (*seen, selector.id))
        if isinstance(selector, ast.Constant):
            return selector.value
        if (
            isinstance(selector, ast.Subscript)
            and isinstance(selector.value, ast.Name)
            and selector.value.id == "params"
            and isinstance(selector.slice, ast.Constant)
        ):
            return params.get(selector.slice.value)
        if (
            isinstance(selector, ast.Call)
            and isinstance(selector.func, ast.Attribute)
            and isinstance(selector.func.value, ast.Name)
            and selector.func.value.id == "params"
            and selector.func.attr == "get"
            and selector.args
            and isinstance(selector.args[0], ast.Constant)
        ):
            default = (
                selector.args[1].value
                if len(selector.args) > 1 and isinstance(selector.args[1], ast.Constant)
                else None
            )
            return params.get(selector.args[0].value, default)
        return None

    relations = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "traverse"
        ):
            continue
        selector = (
            node.args[1]
            if len(node.args) > 1
            else next((kw.value for kw in node.keywords if kw.arg == "relation"), None)
        )
        value = resolve(selector)
        if isinstance(value, str) and value not in relations:
            relations.append(value)
    return relations


def _traversal_shape(asset):
    """Resolve parameter dependencies, not spelling conventions, for traverse inputs."""
    tree = ast.parse(asset.content)
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name):
            assignments.setdefault(node.target.id, []).append(node.iter)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in ("append", "extend")
        ):
            assignments.setdefault(node.func.value.id, []).extend(node.args)

    def keys(expr, seen=()):
        if expr is None:
            return set()
        if isinstance(expr, ast.Name):
            if expr.id in seen:
                return set()
            return set().union(*(keys(v, (*seen, expr.id)) for v in assignments.get(expr.id, ())))
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id in ("nodes", "search", "semantic_search", "traverse")
        ):
            return set()  # Operator filters are not externally supplied traversal seeds.
        if (
            isinstance(expr, ast.Subscript)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "params"
            and isinstance(expr.slice, ast.Constant)
        ):
            return {expr.slice.value} if isinstance(expr.slice.value, str) else set()
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and isinstance(expr.func.value, ast.Name)
            and expr.func.value.id == "params"
            and expr.func.attr == "get"
            and expr.args
            and isinstance(expr.args[0], ast.Constant)
        ):
            return {expr.args[0].value} if isinstance(expr.args[0].value, str) else set()
        if isinstance(expr, ast.Subscript):
            return keys(expr.value, seen)  # slice limits/indexes do not supply node identities
        return set().union(*(keys(child, seen) for child in ast.iter_child_nodes(expr)))

    seeds = set()
    directions = set()
    fixed = set()
    filters = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "nodes":
            selector = next((kw.value for kw in node.keywords if kw.arg == "filters"), None)
            if isinstance(selector, ast.Dict):
                for field, value in zip(selector.keys, selector.values):
                    if isinstance(field, ast.Constant) and isinstance(field.value, str):
                        for key in keys(value):
                            filters[key] = field.value
        if node.func.id != "traverse":
            continue
        seed = (
            node.args[0]
            if node.args
            else next((kw.value for kw in node.keywords if kw.arg == "node_ids"), None)
        )
        direction = (
            node.args[2]
            if len(node.args) > 2
            else next(
                (kw.value for kw in node.keywords if kw.arg == "direction"), ast.Constant("out")
            )
        )
        seeds.update(keys(seed))
        directions.update(keys(direction))
        if isinstance(direction, ast.Constant) and direction.value in ("in", "out"):
            fixed.add(direction.value)
    return seeds, directions, fixed, filters


def _traversal_directions(asset):
    _, keys, fixed, _ = _traversal_shape(asset)
    enabled = set(fixed)
    for key in keys:
        spec = asset.input_contract.get("properties", {}).get(key, {})
        enabled.update(set(spec.get("enum", ("in", "out"))) & {"in", "out"})
    return enabled or {"out"}


def _pressure_graph(asset, params, graph, tag):
    """Exercise internally selected seeds under adversarial row order, on a graph copy.

    Node/edge attributes, sources and vector memory IDs are unchanged. Public row IDs
    are rebuilt together with the copy; external-addressed functions use the original.
    """
    if not tag.startswith("high_degree_") or _traversal_shape(asset)[0]:
        return graph
    import networkx as nx
    from darwinagent.contracts import GraphResult

    direction = tag.removeprefix("high_degree_")
    edges = graph.graph.in_edges if direction == "in" else graph.graph.out_edges
    relations = _traversal_relations(asset, params)
    degree = lambda n: sum(
        not relations or attrs.get("relation") in relations for _, _, attrs in edges(n, data=True)
    )
    ordered = sorted(graph.graph.nodes, key=degree, reverse=True)
    copy = nx.MultiDiGraph()
    copy.graph.update(graph.graph.graph)
    copy.add_nodes_from((n, dict(graph.graph.nodes[n])) for n in ordered)
    copy.add_edges_from(
        (u, v, k, dict(a)) for u, v, k, a in graph.graph.edges(keys=True, data=True)
    )
    return GraphResult(
        nx.freeze(copy),
        getattr(graph, "sources", {}),
        getattr(graph, "raw_outputs", ()),
        getattr(graph, "diagnostics", ()),
        getattr(graph, "vector", None),
    )


def _samples(asset, graph):
    """Prefer actual registered parameters and bounded, contract-valid risk variants."""
    from .trials import stress_trial_samples

    bases = [plain(v) for v in asset.trial_inputs]
    caps = DataCapabilities(graph)
    rows = list(caps.rows.values())
    seed_keys, direction_keys, fixed_directions, filter_keys = _traversal_shape(asset)
    options = [("stress", v) for v in stress_trial_samples(asset.trial_inputs, graph)]
    if rows:
        # A seed with a broad scalar filter can scan the entire graph.
        for base in bases[:3]:
            wide = {
                k: (
                    ""
                    if isinstance(v, str)
                    and k
                    not in (
                        {"node_id", "relation", "direction", "query", "anchor_iso", "expression"}
                        | seed_keys
                        | direction_keys
                    )
                    else v
                )
                for k, v in base.items()
            }
            options.append(("wide_filter", wide))
            if "rows" in base and isinstance(base["rows"], list):
                options.append(("large_rows", {**base, "rows": rows}))
            if "traverse" in asset.content:
                graph_edges = graph.graph
                row_for_node = {actual: rid for rid, actual in caps.actual_ids.items()}
                relations = _traversal_relations(asset, base) or [None]
                allowed_directions = _traversal_directions(asset)
                for direction in sorted(allowed_directions):
                    edges = graph_edges.in_edges if direction == "in" else graph_edges.out_edges
                    for relation in relations:
                        degree = {
                            n: sum(
                                relation is None or attrs.get("relation") == relation
                                for _, _, attrs in edges(n, data=True)
                            )
                            for n in graph_edges.nodes
                        }
                        ranked = sorted(degree, key=degree.get, reverse=True)
                        if not ranked or degree[ranked[0]] == 0:
                            # Unknown/absent relation must still face a nonempty-edge test.
                            ranked = sorted(graph_edges.nodes, key=graph_edges.degree, reverse=True)
                        if not ranked:
                            continue
                        target = caps.rows.get(row_for_node.get(ranked[0]))
                        if target:
                            params = {**base}
                            for key in seed_keys:
                                value = base.get(key)
                                spec = asset.input_contract.get("properties", {}).get(key, {})
                                if isinstance(value, (list, tuple)) or spec.get("type") == "array":
                                    items = spec.get("items", {})
                                    row_input = items.get("type") != "string" and (
                                        not value or isinstance(value[0], Mapping)
                                    )
                                    params[key] = [target] if row_input else [target["node_id"]]
                                else:
                                    params[key] = target["node_id"]
                            for key in direction_keys:
                                params[key] = direction
                            if not seed_keys:
                                # Internal nodes(filters=...) selectors get real graph values.
                                for key, field in filter_keys.items():
                                    if field in target:
                                        params[key] = target[field]
                            options.append((f"high_degree_{direction}", params))
                            if not seed_keys:
                                options.append(
                                    (
                                        f"high_degree_{direction}",
                                        {**base, **{key: direction for key in direction_keys}},
                                    )
                                )
            if "relative_date" in asset.content:
                options.append(
                    (
                        "relative_date_object",
                        {
                            **base,
                            **({"anchor_iso": "2024-05-08"} if "anchor_iso" in base else {}),
                            **({"expression": "上周日"} if "expression" in base else {}),
                            **({"rows": rows[:100]} if isinstance(base.get("rows"), list) else {}),
                        },
                    )
                )
    seen = set()
    # 遍历种子参数（AST 解析，别名/中文键同样命中）必须是图内节点——空串/未知值
    # 的压力变体在生成端即非法（2026-10-05：逐字段清空变体曾把空种子送进试跑）。
    seed_keys, _dk, _fd, _flt = _traversal_shape(asset)
    for tag, params in options:
        params = plain(params)
        try:
            validate_value(params, asset.input_contract, "tool.params")
        except ValueError:
            continue
        if isinstance(params.get("node_id"), str) and params["node_id"] not in caps.rows:
            continue
        if any(
            isinstance(params.get(k), str) and params.get(k) not in caps.rows for k in seed_keys
        ):
            continue
        if (
            "semantic_search" in asset.content
            and "query" in params
            and not str(params["query"]).strip()
        ):
            continue
        if "relative_date" in asset.content and "anchor_iso" in params:
            try:
                date.fromisoformat(params["anchor_iso"])
            except (TypeError, ValueError):
                continue
            if not str(params.get("expression", "")).strip():
                continue
        if isinstance(params.get("rows"), list) and any(
            isinstance(r, dict) and r.get("node_id") is not None and r["node_id"] not in caps.rows
            for r in params["rows"]
        ):
            continue
        key = (tag, digest(params))
        if key not in seen:
            seen.add(key)
            yield tag, params


def admit_candidate(
    bundle,
    training_cases,
    graph_refs,
    config,
    required_caps,
    report_path,
    replay_inputs=(),
    remote_vector_error=None,
    answer_contract=None,
    replay_checks=(),
    answer_counterexamples=(),
    answer_examples=(),
):
    """Run independent required checks, persist the complete report, then reject on gaps."""
    report_path = Path(report_path)
    limits = Limits(config.function_steps, config.function_timeout_s, config.result_bytes)
    assets = tuple(bundle.assets.assets)
    framework = snapshot_files([Path(__file__).resolve().parents[1]])
    report = {
        "schema_version": 1,
        "candidate_version": bundle.version,
        "asset_fingerprints": {a.id: a.fingerprint for a in assets},
        "config_digest": digest(config.to_dict()),
        "framework_digest": digest(framework),
        "sandbox_digest": framework[
            str(Path(__file__).resolve().parents[1] / "operators" / "sandbox.py")
        ],
        "graph_digests": {},
        "snapshot_digests": {},
        "scenarios": [],
        "counts": {},
        "verdict": "incomplete",
    }
    scenarios = report["scenarios"]
    base_records = []
    started = time.monotonic()
    try:
        validate_bundle(bundle)
    except Exception as exc:
        scenarios.append(
            _row(None, "static", "failed", error_type=type(exc).__name__, error=str(exc))
        )
        atomic_json(report_path, report)
        raise AdmissionError(report_path, report) from exc
    scenarios.append(_row(None, "static", "passed"))
    if remote_vector_error:
        scenarios.append(_row(None, "remote_vector", "incomplete", error=remote_vector_error))
    registry = FunctionRegistry(bundle, limits)
    checks = CheckRegistry(bundle, limits)
    cases = list(training_cases)
    if not cases or not graph_refs:
        scenarios.append(_row(None, "training_graph", "incomplete", error="No training graph"))
    for case in cases:
        graph = graph_refs.get(case.id)
        if graph is None:
            scenarios.append(
                _row(
                    None,
                    "training_graph",
                    "incomplete",
                    ref=case.id,
                    error="Missing training graph",
                )
            )
            continue
        report["graph_digests"][case.id] = _graph_identity(graph)
        manifest = next(
            (d for d in graph.diagnostics if isinstance(d, dict) and d.get("snapshot_digest")), None
        )
        if manifest:
            report["snapshot_digests"][case.id] = manifest["snapshot_digest"]
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
        atomic_json(report_path, report)
        for entry in replay_inputs:
            if len(entry) == 3:
                replay_case, aid, params = entry
                if replay_case != case.id:
                    continue
            else:
                aid, params = entry
            asset = registry.functions.get(aid, (None, None))[0]
            if asset is None:
                scenarios.append(
                    _row(
                        None,
                        "replay",
                        "incomplete",
                        ref=f"{case.id}:{aid}",
                        error="Historical asset not registered",
                    )
                )
                continue
            ref = f"{case.id}:replay:{digest(plain(params))}"
            if remote_vector_error and "semantic_search" in asset.content:
                scenarios.append(
                    _row(
                        asset,
                        "replay",
                        "incomplete",
                        ref=ref,
                        error="Remote vector dependency unavailable: " + remote_vector_error,
                    )
                )
                continue
            try:
                validate_value(params, asset.input_contract, "tool.params")
            except ValueError as exc:
                scenarios.append(
                    _row(
                        asset,
                        "replay",
                        "incomplete",
                        ref=ref,
                        error="Historical parameters need a validated contract migration: "
                        + str(exc),
                    )
                )
                continue
            observation = {}
            try:
                outcome = registry.call(aid, params, graph, _observation=observation)
                scenarios.append(
                    _row(
                        asset,
                        "replay",
                        "passed",
                        ref=ref,
                        data_digest=digest(plain(outcome["data"])),
                        node_ids=outcome["node_ids"],
                        read_node_ids=outcome["read_node_ids"],
                        source_ids=outcome["source_ids"],
                        **observation,
                    )
                )
            except Exception as exc:
                scenarios.append(
                    _row(
                        asset,
                        "replay",
                        "failed",
                        ref=ref,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        **observation,
                    )
                )
            atomic_json(report_path, report)
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
            atomic_json(report_path, report)
        composable = {}
        for aid, (asset, _) in sorted(registry.functions.items()):
            if remote_vector_error and "semantic_search" in asset.content:
                report["counts"].setdefault(case.id, {}).setdefault(
                    aid,
                    {
                        "base": len(asset.trial_inputs),
                        "stress_generated": 0,
                        "stress_legal": 0,
                        "executed": 0,
                        "by_scenario": {},
                    },
                )
                scenarios.append(
                    _row(
                        asset,
                        "remote_vector",
                        "incomplete",
                        ref=case.id,
                        error="Required semantic search trials not executed: "
                        + remote_vector_error,
                    )
                )
                continue
            try:
                registry.call(aid, None, graph)
            except ValueError as exc:
                scenarios.append(
                    _row(
                        asset,
                        "invalid_params_rejected",
                        "passed" if str(exc).startswith("tool.params:") else "failed",
                        ref=f"{case.id}:invalid:null",
                        error=None if str(exc).startswith("tool.params:") else str(exc),
                    )
                )
            except Exception as exc:
                scenarios.append(
                    _row(
                        asset,
                        "invalid_params_rejected",
                        "failed",
                        ref=f"{case.id}:invalid:null",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                )
            else:
                scenarios.append(
                    _row(
                        asset,
                        "invalid_params_rejected",
                        "failed",
                        ref=f"{case.id}:invalid:null",
                        error="Non-object parameters were accepted",
                    )
                )
            counts = (
                report["counts"]
                .setdefault(case.id, {})
                .setdefault(
                    aid,
                    {
                        "base": 0,
                        "stress_generated": 0,
                        "stress_legal": 0,
                        "executed": 0,
                        "by_scenario": {},
                    },
                )
            )
            static_errors = loop_carried_capability_errors(asset.content)
            if static_errors:
                scenarios.append(
                    _row(
                        asset, "loop_carried", "failed", ref=case.id, error="; ".join(static_errors)
                    )
                )
            else:
                # 通过也落行：场景行集完整，verified_fix 的「最初失败场景复现通过」
                # 绑定才能机器判定（2026-10-05 审查 P2）。
                scenarios.append(_row(asset, "loop_carried", "passed", ref=case.id))
            required = [("base", plain(v)) for v in asset.trial_inputs]
            generated = list(_samples(asset, graph))
            counts["base"] = len(required)
            counts["stress_generated"] = len(generated)
            if not generated:
                scenarios.append(
                    _row(
                        asset,
                        "stress_coverage",
                        "incomplete",
                        ref=case.id,
                        error="No contract-valid stress sample",
                    )
                )
            # Reserve a representative of each shape before optional variations.
            representatives = {}
            for tag, params in generated:
                representatives.setdefault(tag, params)
            selected_stress = list(representatives.items())
            if len(selected_stress) < 32:
                selected_stress.extend(
                    (t, p) for t, p in generated if (t, p) not in selected_stress
                )
            selected_stress = selected_stress[:32]
            selected_tags = {tag for tag, _ in selected_stress}
            expected_tags = set()
            if "relative_date" in asset.content:
                expected_tags.add("relative_date_object")
            if "traverse" in asset.content:
                _, direction_keys, fixed_directions, _ = _traversal_shape(asset)
                enabled = _traversal_directions(asset)
                for direction in ("in", "out"):
                    tag = f"high_degree_{direction}"
                    if direction not in enabled:
                        scenarios.append(
                            _row(
                                asset,
                                tag,
                                "skipped",
                                required=False,
                                ref=case.id,
                                reason="Traversal code does not expose this direction",
                            )
                        )
                        continue
                    degree = graph.graph.in_degree if direction == "in" else graph.graph.out_degree
                    if any(degree(node) > 0 for node in graph.graph.nodes):
                        expected_tags.add(tag)
                    else:
                        scenarios.append(
                            _row(
                                asset,
                                tag,
                                "skipped",
                                required=False,
                                ref=case.id,
                                reason=f"Training graph has no {direction} edges",
                            )
                        )
            for tag in sorted(expected_tags - selected_tags):
                scenarios.append(
                    _row(
                        asset,
                        tag,
                        "incomplete",
                        ref=case.id,
                        error="No contract-valid input triggered the required capability path",
                    )
                )
            counts["stress_legal"] = len(selected_stress)
            selected = required + selected_stress
            for index, (tag, params) in enumerate(selected):
                ref = f"{case.id}:{tag}:{index}:{digest(params)}"
                observation = {}
                try:
                    trial_graph = _pressure_graph(asset, params, graph, tag)
                    if trial_graph is not graph:
                        observation["pressure_kind"] = "graph_row_order"
                        observation["pressure_graph_digest"] = _graph_identity(trial_graph)
                    outcome = registry.call(aid, params, trial_graph, _observation=observation)
                    status, error_type, error = "passed", None, None
                except Exception as exc:
                    outcome = None
                    status, error_type, error = "failed", type(exc).__name__, str(exc)
                expected_capability = (
                    "relative_date"
                    if tag == "relative_date_object"
                    else "traverse"
                    if tag.startswith("high_degree_")
                    else None
                )
                if (
                    outcome is not None
                    and expected_capability
                    and not outcome["capability_calls"].get(expected_capability)
                ):
                    status, error_type, error = (
                        "incomplete",
                        "CoverageGap",
                        (f"{tag} did not invoke {expected_capability}"),
                    )
                if (
                    outcome is not None
                    and tag.startswith("high_degree_")
                    and tag.removeprefix("high_degree_") not in outcome["traverse_directions"]
                ):
                    status, error_type, error = (
                        "incomplete",
                        "CoverageGap",
                        (f"{tag} did not traverse in the requested direction"),
                    )
                if (
                    outcome is not None
                    and tag.startswith("high_degree_")
                    and not any(
                        call["direction"] == tag.removeprefix("high_degree_")
                        and call["matched_edges"] > 0
                        for call in observation.get("traverse_observations", ())
                    )
                ):
                    status, error_type, error = (
                        "incomplete",
                        "CoverageGap",
                        (f"{tag} did not traverse any matching relation edge"),
                    )
                counts["executed"] += 1
                covered = counts["by_scenario"].setdefault(tag, {"executed": 0, "passed": 0})
                covered["executed"] += 1
                covered["passed"] += status == "passed"
                scenarios.append(
                    _row(
                        asset,
                        tag,
                        status,
                        ref=ref,
                        error_type=error_type,
                        error=error,
                        parameters=plain(params),
                        **observation,
                    )
                )
                if outcome is not None:
                    scenarios[-1]["capability_calls"] = outcome["capability_calls"]
                    scenarios[-1]["read_node_ids"] = outcome["read_node_ids"]
                    scenarios[-1]["node_ids"] = outcome["node_ids"]
                    scenarios[-1]["source_ids"] = outcome["source_ids"]
                    if tag == "base":
                        base_records.append(outcome)
                    if (
                        status == "passed"
                        and trial_graph is graph
                        and _returned_rows(outcome["data"])
                    ):
                        composable.setdefault(aid, (outcome, _returned_rows(outcome["data"])))
            # Some legal selectors return no relation hits. They are discovery trials,
            # not execution failures. Require an actually covered pressure path per
            # direction across the legal selectors; never waive runtime failures.
            for tag in sorted(expected_tags):
                attempts = [
                    r
                    for r in scenarios
                    if r.get("asset_id") == aid
                    and r.get("scenario_id") == tag
                    and str(r.get("input_ref", "")).startswith(case.id + ":")
                ]
                gaps = [r for r in attempts if r.get("error_type") == "CoverageGap"]
                if any(r["status"] == "passed" for r in attempts):
                    for row in gaps:
                        row["required"] = False
                        row["reason"] = "Other legal selector actually exercised this pressure path"
            if counts["executed"] < counts["base"] + counts["stress_legal"]:
                scenarios.append(
                    _row(
                        asset,
                        "stress_coverage",
                        "incomplete",
                        ref=case.id,
                        error="Not all generated legal samples executed",
                    )
                )
            atomic_json(report_path, report)
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
        atomic_json(report_path, report)
    if required_caps:
        problems = trial_capability_floor_errors(base_records, required_caps)
        scenarios.append(
            _row(
                None,
                "capability_floor",
                "failed" if problems else "passed",
                error=str(problems) if problems else None,
            )
        )
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    report["verdict"] = (
        "passed"
        if scenarios and all(x["status"] == "passed" or not x["required"] for x in scenarios)
        else "failed"
    )
    atomic_json(report_path, report)
    if report["verdict"] != "passed":
        raise AdmissionError(report_path, report)
    return report
