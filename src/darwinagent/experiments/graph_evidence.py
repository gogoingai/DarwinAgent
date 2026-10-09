"""Observable graph structure for optimization; these are clues, not causal verdicts."""

from __future__ import annotations

import json
from collections import Counter

import networkx as nx

from darwinagent.runtime.artifacts import digest


def graph_evidence(graph, schema, previous=None):
    graph = graph.graph if hasattr(graph, "sources") else graph
    types = Counter(d.get("etype") for _, d in graph.nodes(data=True))
    relations = Counter(d.get("relation") for *_, d in graph.edges(data=True))
    isolated = []
    for nid, nd in graph.nodes(data=True):
        if nd.get("etype") in ("原子事实", "AtomicFact") and not graph.degree(nid):
            key = json.loads(nd.get("__key__", "{}"))
            isolated.append(key.get("编号", key.get("id", nid)))
    result = {
        "graph_digest": digest(nx.node_link_data(graph, edges="edges")),
        "schema_fingerprint": digest(schema.to_yaml()),
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "node_types": dict(types),
        "relation_types": dict(relations),
        "unmaterialized_types": [e.name for e in schema.entities if not types[e.name]],
        "unmaterialized_relations": [r.name for r in schema.relations if not relations[r.name]],
        "facts_without_relations": isolated[:30],
        "facts_without_relations_total": len(isolated),
        "causal_status": "observation_only",
        "interpretation": "缺少实例或孤立事实可能源于无对应证据；须查询原件后判断 S、构图提示或 F 是否需要修改。",
    }
    if previous:
        result["delta"] = {k: result[k] - previous[k] for k in ("nodes", "edges")}
        result["previous_graph_digest"] = previous["graph_digest"]
    return result


ASSET_DIAGNOSIS_PROTOCOL = """先定位需要修改的资产，再提出补丁。
S：检查任务所需的实体、属性、关系是否被声明并在实际图中物化；结合原始事实判断，缺实例不自动等于 S 错。
F：检查参数契约、执行错误、检索范围、空结果、截断与关系遍历；区分图中没有信息和工具没有取到信息。
C：检查实际输入、检查意见和来源，判断是合理拦截还是规则误拦；不得为提高分数删除正确检查。
P：检查构图、检索、作答、审查提示是否正确使用已有结构与证据。
Wiki 的 asset_change_signals 是待验证线索。证据不足时先 query_wiki 查询原件与历史反例。
每个 patch.reason 说明观察、候选原因、拟改资产和预期可测变化；复用失败经验，避免无依据试改。
S 与 P.extract 变化在 LLM 构图模式会重建图，F/C/其他 P 变化复用相同图；以 pipeline_active_stages 为准。
"""


def asset_change_signals(feedback):
    signals = []

    def add(kinds, observation, evidence, next_check, case_id=None, question_id=None):
        signals.append(
            {
                "asset_kinds": kinds,
                "observation": observation,
                "evidence": evidence,
                "next_check": next_check,
                "case_id": case_id,
                "question_id": question_id,
                "confidence": "hypothesis",
            }
        )

    for graph in feedback.get("graphs", []):
        if (
            graph.get("unmaterialized_types")
            or graph.get("unmaterialized_relations")
            or graph.get("facts_without_relations_total")
        ):
            observed = {
                k: graph[k]
                for k in (
                    "graph_digest",
                    "unmaterialized_types",
                    "unmaterialized_relations",
                    "facts_without_relations",
                    "facts_without_relations_total",
                )
                if k in graph
            }
            add(
                ["S", "P"],
                "部分声明结构未物化或事实没有关系",
                observed,
                "查询对应原始事实与图原件，检查 S 是否能表达该事实、P.extract 是否正确构图；无对应事实时不应补造。",
                graph.get("case_id"),
            )
    for graph in feedback.get("graph_diagnostics", []):
        if graph.get("status") == "execution_error":
            add(
                ["S", "P"],
                "构图阶段失败",
                graph,
                "检查构图原始响应、S 类型与来源校验错误；先排除传输故障。",
                graph.get("case_id"),
            )
    for failure in feedback.get("generation_failures", []):
        if failure.get("fault_category") == "review_exhausted":
            add(
                ["P"],
                "审查拒绝耗尽修订次数",
                failure,
                "查询同题候选、来源与历次审查意见，检查 P.review 是否前后矛盾、P.answer 是否落实有效反馈；不得绕过正确审查或把执行失败改成拒答。",
                failure.get("case_id"),
                failure.get("question_id"),
            )
    for row in feedback.get("diagnostics", []):
        trace = row.get("trace", {})
        diag = row.get("diagnostic", {})
        case_id, qid = row.get("case_id"), diag.get("question_id")
        if trace.get("tool_errors") or trace.get("empty_results"):
            add(
                ["F", "S"],
                "工具调用失败或检索返回空",
                {
                    "tool_errors": trace.get("tool_errors", [])[:2],
                    "empty_results": trace.get("empty_results", 0),
                },
                "查询实际参数、工具源码与图邻域，区分工具故障、检索遗漏和图缺结构。",
                case_id,
                qid,
            )
        if trace.get("rejections"):
            add(
                ["C", "P"],
                "检查或审查拒绝了候选",
                trace["rejections"],
                "查询被检查的实际输入、规则与原始证据，区分合理拦截、规则误拦和提示执行偏差。",
                case_id,
                qid,
            )
        if diag.get("precise") is False or diag.get("status") == "abstained":
            add(
                ["P", "F", "S"],
                "答案未通过训练评测或拒答",
                {
                    "status": diag.get("status"),
                    "precise": diag.get("precise"),
                    "returned_rows": trace.get("returned_rows", 0),
                    "empty_results": trace.get("empty_results", 0),
                },
                "沿原始事实→实际图→工具返回→候选答案→检查意见定位首次信息缺失或偏差；不要仅凭分数归因。",
                case_id,
                qid,
            )
    return signals
