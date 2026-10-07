"""Contract-valid samples and actual-graph pressure variants for admission."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import date

from darwinagent.contracts import plain
from darwinagent.kernel.spec import validate_value
from darwinagent.operators.data import DataCapabilities
from darwinagent.runtime.artifacts import digest


def _graph_identity(graph):
    # GraphResult is not JSON; the sorted data-bearing rows provide a stable identity.
    return digest(sorted(DataCapabilities(graph).rows.values(), key=lambda r: r["node_id"]))


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

    def degree(n):
        return sum(
            not relations or attrs.get("relation") in relations
            for _, _, attrs in edges(n, data=True)
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
