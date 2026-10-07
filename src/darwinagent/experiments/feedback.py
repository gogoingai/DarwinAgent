"""Experiment feedback helpers; independent of the controller."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from darwinagent.contracts import plain
from darwinagent.kernel.revision import training_id

from .wiki import bounded_trace

FEEDBACK_BUDGET_CHARS = 35000
_DIAG_ROW_CHARS = 2200
_TRACE_CHARS = 600


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _diagnostic_failure(row):
    """失败优先：answered 且判分 precise 通过（或显式 passed）才算成功，其余进反馈。"""
    if not isinstance(row, dict):
        return True
    if row.get("passed") is True:
        return False
    if row.get("status") == "answered":
        original = row.get("original") if isinstance(row.get("original"), dict) else {}
        if original.get("precise") is True:
            return False
    return True


def _compact_diagnostic(row):
    """压缩判分原始输出：只保留归因所需字段；金标（reference）与判题内部结构不进提案载荷。"""
    original = (
        row.get("original")
        if isinstance(row, dict) and isinstance(row.get("original"), dict)
        else None
    )
    if original is None:
        blob = json.dumps(row, ensure_ascii=False, default=str)
        if len(blob) <= _DIAG_ROW_CHARS:
            return row
        return {"_row_truncated": blob[:_DIAG_ROW_CHARS]}
    keep = {
        "question_id": row.get("question_id"),
        "question": _clip(row.get("question"), 120),
        "status": row.get("status"),
        "precise": original.get("precise"),
        "lenient": original.get("lenient"),
    }
    if row.get("answer"):
        keep["answer"] = _clip(row.get("answer"), 200)
    if row.get("error"):
        keep["error"] = _clip(row.get("error"), 200)
    for src in ("missing_elements", "wrong_elements", "precision_issues"):
        items = original.get(src) or []
        if items:
            keep[src] = [_clip(i, 60) for i in items[:5]]
    return keep


def _retrieval_trace(answer):
    """单题执行轨迹摘要（评审②）：从结构化 AnswerResult.trace 提取。参数优先取工具事件里
    执行点记录的 parameters（协议重试中被拒动作不会错配）；旧记录回退按 asset_id 顺序配对
    成功调用。含证据摘录与来源标识、空结果、截断标记（tools_truncated）、拒绝理由。"""
    events = plain([ev for ev in (getattr(answer, "trace", ()) or ())])  # mappingproxy 全解包
    raw_outputs = getattr(answer, "raw_outputs", ()) or ()
    fallback_calls = []
    has_ready = False
    for raw in raw_outputs:
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj.get("action") == "call":
            fallback_calls.append(obj)
        elif isinstance(obj, dict) and obj.get("action") == "ready":
            has_ready = True
    fb_index = 0
    tools = []
    returned_rows = 0
    empty_results = 0
    seen_ids = set()
    tool_keys = set()
    for ev in events:
        stage = ev.get("stage")
        if stage == "retrieval":
            rows = ev.get("rows", 0)
            tools.append({"tool": "vector_once", "rows": rows, "k": ev.get("k")})
            returned_rows += rows
            if not rows:
                empty_results += 1
            continue
        if stage != "tool":
            continue
        params = ev.get("parameters")
        if params is None:
            while fb_index < len(fallback_calls):
                action = fallback_calls[fb_index]
                fb_index += 1
                if action.get("asset_id") == ev.get("asset_id"):
                    params = action.get("parameters")
                    break
        # 工具返回类型分流（评审四）：数组取证据行；对象/数值/字符串/布尔是框架允许的
        # 合法返回，保留有界摘要且不算空结果；None 才是空。摘要永不因合法返回类型而失败。
        data = ev.get("data")
        entry = {"tool": ev.get("asset_id"), "capabilities": ev.get("capability_calls") or None}
        if isinstance(data, list):
            rows = len(data)
            if not rows:
                empty_results += 1
            excerpts = []
            for row in data[:2]:
                if not isinstance(row, Mapping):
                    excerpts.append({"value": _clip(row, 60)})
                    continue
                excerpts.append(
                    {
                        "node_id": row.get("node_id"),
                        "statement": _clip(
                            row.get("陈述") or row.get("statement") or row.get("名称"), 60
                        ),
                        "source_ids": list(row.get("source_ids") or ())[:2],
                    }
                )
            if excerpts:
                entry["evidence"] = excerpts
        else:
            rows = 1 if data is not None else 0
            if data is None:
                empty_results += 1
            else:
                entry["returns"] = _clip(data, 90)  # 合法标量/对象返回（0/False 非空）
        entry["rows"] = rows
        returned_rows += rows
        if params is not None:
            entry["params"] = _clip(params, 90)
        # 图新增遥测（专家规格#2，反馈层事后差分——不触碰作答路径）：本调用新召回的
        # node_id（对前一调用集合的差集）与重复调用标记（同工具同参数再现）。
        known = set(seen_ids)
        new_ids = [nid for nid in (ev.get("node_ids") or ()) if nid not in known]
        seen_ids.update(ev.get("node_ids") or ())
        entry["new_node_ids"] = new_ids[:8]
        call_key = (
            ev.get("asset_id"),
            json.dumps(params, sort_keys=True, ensure_ascii=False)
            if isinstance(params, Mapping)
            else None,
        )
        entry["repeat_call"] = call_key in tool_keys
        tool_keys.add(call_key)
        tools.append(entry)
    rejections = []
    tool_errors = []
    for ev in events:
        stage = ev.get("stage")
        if stage == "tool_error":
            tool_errors.append(
                {
                    "tool": ev.get("asset_id"),
                    "input_ref": ev.get("input_ref"),
                    "error_type": ev.get("error_type"),
                    "error": _clip(ev.get("error"), 150),
                    "observation": ev.get("observation"),
                }
            )
        if stage == "review" and not ev.get("accepted"):
            rejections.append(
                {"by": "review", "reason": _clip(ev.get("feedback") or ev.get("reason"), 150)}
            )
        elif stage == "candidate":
            for check in (ev.get("checks") or ())[:6]:
                if isinstance(check, Mapping) and not check.get("ok"):
                    rejections.append(
                        {
                            "by": "check",
                            "check_id": check.get("check_id"),
                            "issues": _clip(check.get("issues"), 120),
                        }
                    )
    # 停止信号＝工具循环的收尾方式：ready 动作 vs 步数耗尽；执行错误覆盖之
    stopped = "ready" if has_ready else "budget"
    if any(ev.get("stage") == "execution_error" for ev in events):
        stopped = "execution_error"
    trace = {
        "question_id": answer.question_id,
        "status": answer.status,
        "tool_calls": len(tools),
        "model_calls": len(raw_outputs),
        "returned_rows": returned_rows,
        "empty_results": empty_results,
        "stopped": stopped,
        "tools": tools,
        "tool_errors": tool_errors[:2],
        "rejections": rejections,
    }
    if getattr(answer, "error", None):
        trace["error"] = _clip(answer.error, 150)
    truncated = 0
    while len(tools) > 2 and len(json.dumps(trace, ensure_ascii=False, default=str)) > _TRACE_CHARS:
        tools.pop()
        truncated += 1
    if truncated:
        trace["tools_truncated"] = truncated
    return plain(trace)


def pipeline_active_stages(snapshot_root):
    """资产职责图（专家规格#5）：让提案器明确知道每个资产在当前配置下的实际执行情况——
    冻结快照下 P.extract 根本不执行、S 只作用于查询词表层（不重建图）；修改它们不会
    改变本轮计分路径，不得把这类改动算作答题收益。"""
    if snapshot_root is not None:
        return {
            "P.extract": "SKIPPED——记忆由冻结快照供给，改它不进本轮计分路径",
            "P.tools": "执行中（检索决策）",
            "P.answer": "执行中（作答）",
            "P.review": "执行中（审查）",
            "S": "仅查询词表层——冻结图不因 S 补丁重建，新类型在图中无数据",
            "F": "执行中（检索函数）",
            "C": "执行中（结构检查，电池准入）",
        }
    return {
        "P.extract": "执行中（语料抽取）",
        "P.tools": "执行中",
        "P.answer": "执行中",
        "P.review": "执行中",
        "S": "全量生效（驱动抽取）",
        "F": "执行中",
        "C": "执行中",
    }


def training_feedback(
    cases,
    results,
    case_diagnostics,
    baseline,
    active_stages=None,
    previous_round=None,
    budget=FEEDBACK_BUDGET_CHARS,
):
    """Failure-first proposal feedback: compressed diagnostics (gold references never enter
    the payload) plus a per-question execution trace. One character budget bounds the COMPLETE
    serialized payload. With several training cases the budget rotates case by case — an early
    case may not crowd the others out. Answer association is keyed per case (评审#3):
    same-named question ids in different cases never share a trace."""
    per_case_rows = []
    rows_total = 0
    for (case_id, diagnostics), result in zip(case_diagnostics, results):
        answers = {a.question_id: a for a in result.answers}  # 会话内索引：同名题号跨对话不串用
        rows = []
        for row in plain(diagnostics):
            if not _diagnostic_failure(row):
                continue
            rows_total += 1
            unit = {"case_id": case_id, "diagnostic": _compact_diagnostic(row)}
            answer = answers.get(row.get("question_id") if isinstance(row, dict) else None)
            if answer is not None:
                unit["trace"] = _retrieval_trace(answer)
            rows.append(unit)
        per_case_rows.append(rows)
    failures = []
    for case, result in zip(cases, results):
        failures += [
            {"case_id": case.id, "question_id": a.question_id, "error": a.error}
            for a in result.answers
            if a.status == "execution_error"
        ]
    graph_rows = []
    for result in results:
        graph_rows += list(plain(result.graph_diagnostics))
    score_data = baseline.to_dict()
    score_data.pop("diagnostics", None)  # 诊断单独装订，载荷不重复计费

    def payload(case_counts, fail_count, graph_count):
        diagnostics = [row for rows, take in zip(per_case_rows, case_counts) for row in rows[:take]]
        return {
            "scores": score_data,
            "pipeline_active_stages": active_stages,
            "previous_round": previous_round,
            "diagnostics": diagnostics,
            "diagnostic_rows_total": rows_total,
            "diagnostic_rows_in_proposal": len(diagnostics),
            "generation_failures": failures[:fail_count],
            "generation_failures_total": len(failures),
            "generation_failures_truncated": fail_count != len(failures),
            "graph_diagnostics": graph_rows[:graph_count],
            "feedback_budget_chars": budget,
        }

    def fits(case_counts, fail_count, graph_count):
        return (
            len(
                json.dumps(
                    payload(case_counts, fail_count, graph_count), ensure_ascii=False, default=str
                )
            )
            <= budget
        )

    zero_counts = (0,) * len(per_case_rows)
    skeleton = len(json.dumps(payload(zero_counts, 0, 0), ensure_ascii=False, default=str))
    if skeleton > budget:
        raise ValueError(
            f"反馈骨架（scores+统计字段）序列化后 {skeleton} 字符，超过预算 "
            f"{budget}：评分载荷本身超限，拒绝生成提案"
        )
    # 轮转准入：每步从已入载行数最少的对话取一行——多对话均分预算，谁也不能先占满。
    case_counts = [0] * len(per_case_rows)
    progress = True
    while progress:
        progress = False
        for i in sorted(range(len(per_case_rows)), key=lambda idx: case_counts[idx]):
            if case_counts[i] >= len(per_case_rows[i]):
                continue
            trial = list(case_counts)
            trial[i] += 1
            if fits(tuple(trial), 0, 0):
                case_counts = trial
                progress = True
                break
    fail_count = 0
    while fail_count < len(failures) and fits(tuple(case_counts), fail_count + 1, 0):
        fail_count += 1
    graph_count = 0
    while graph_count < len(graph_rows) and fits(tuple(case_counts), fail_count, graph_count + 1):
        graph_count += 1
    return payload(tuple(case_counts), fail_count, graph_count)


def question_identity(case):
    """Composite training identity: same-named questions in different cases stay distinct,
    and '::' inside either id cannot create collisions (length-prefixed encoding)."""
    return [training_id(case.id, q.id) for q in case.questions]


def _wiki_training_evidence(cases, results, baseline_results=(), diagnostics=()):
    """Read actual training generation/source artifacts, never judge answer content."""
    current = {r.case_id: r for r in results}
    previous = {r.case_id: r for r in baseline_results}
    flags = {}
    for case_id, rows in diagnostics:
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            flags[(case_id, str(row.get("question_id")))] = {
                key: bool(value["precise"])
                for key, value in row.items()
                if key in ("original", "repaired")
                and isinstance(value, Mapping)
                and isinstance(value.get("precise"), bool)
            }
    examples = []
    for case in cases:
        answers = {a.question_id: a for a in getattr(current.get(case.id), "answers", ())}
        old = {a.question_id: a for a in getattr(previous.get(case.id), "answers", ())}
        sources = {b.source.location: b for b in case.corpus}
        sources.update({b.source.id: b for b in case.corpus})
        for q in case.questions:
            a = answers.get(q.id)
            if a is None:
                continue
            pointers = set(e.location for e in a.evidence)
            trace = []
            for ev in plain(a.trace or ()):
                if ev.get("stage") not in ("tool", "tool_error", "candidate", "review"):
                    continue
                item = {
                    k: ev[k]
                    for k in (
                        "stage",
                        "asset_id",
                        "parameters",
                        "error",
                        "error_type",
                        "observation",
                        "capability_calls",
                        "accepted",
                        "feedback",
                        "checks",
                    )
                    if k in ev
                }
                candidate = ev.get("candidate")
                if isinstance(candidate, Mapping):
                    answer = str(candidate.get("answer", ""))
                    try:
                        json_type = type(json.loads(answer)).__name__
                    except (ValueError, TypeError):
                        json_type = "not_json"
                    item["candidate_summary"] = {
                        "status": candidate.get("status"),
                        "answer": answer[:1800],
                        "answer_truncated": len(answer) > 1800,
                        "json_type": json_type,
                        "node_ids": list(candidate.get("node_ids", ()))[:12],
                    }
                data = ev.get("data")
                rows = data.get("rows", ()) if isinstance(data, dict) else data
                if isinstance(rows, (tuple, list)):
                    item["rows"] = list(rows[:3])
                    item["row_count"] = len(rows)
                    for row in rows[:12]:
                        if isinstance(row, Mapping):
                            import re

                            pointers.update(re.findall(r"D\d+:\d+", str(row.get("出处", ""))))
                elif data is not None:
                    item["data"] = data
                encoded = json.dumps(item, ensure_ascii=False)
                if len(encoded) > 3500:
                    item = {"stage": ev["stage"], "summary": encoded[:3500], "truncated": True}
                trace.append(item)
            quote_rows = []
            for pointer in sorted(pointers):
                block = sources.get(pointer)
                if block and len(quote_rows) < 6:
                    quote_rows.append(
                        {
                            "source_id": block.source.id,
                            "location": block.source.location,
                            "text": block.text[:800],
                            "metadata": plain(block.metadata),
                        }
                    )
            prior = old.get(q.id)
            examples.append(
                {
                    "training_id": training_id(case.id, q.id),
                    "question": q.text,
                    "question_parameters": plain(q.parameters),
                    "status": a.status,
                    "generated_answer": a.answer[:1200],
                    "error": a.error,
                    "baseline_answer": prior.answer[:1200] if prior else None,
                    "score_flags": flags.get((case.id, q.id), {}),
                    "trace": bounded_trace(trace, 8),
                    "source_text": quote_rows,
                }
            )
    examples.sort(
        key=lambda e: (
            all(e["score_flags"].values()) if e["score_flags"] else e["status"] == "answered"
        )
    )
    return {"training_examples": examples, "training_examples_total": len(examples)}


def _per_case_feedback_facts(root, name, cases):
    """Per-case diagnostics from the stage's evaluation checkpoints: the aggregated baseline
    loses case attribution, the per-case files keep it."""
    rows = []
    for case in cases:
        path = Path(root) / name / "evaluation" / f"{case.id}.json"
        diagnostics = ()
        if path.exists():
            diagnostics = plain(json.loads(path.read_text())["scores"].get("diagnostics", ()))
        rows.append((case.id, diagnostics))
    return rows
