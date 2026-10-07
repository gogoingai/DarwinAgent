"""Training evidence sanitization and bounded compression."""

from __future__ import annotations


def safe_scores(scores):
    data = scores.to_dict() if hasattr(scores, "to_dict") else scores
    return {
        key: data[key]
        for key in ("metrics", "total", "completed", "generation_faults", "evaluation_faults")
        if key in data
    }


def safe_feedback(feedback):
    """Only allowlisted grading flags and execution traces may reach optimization."""
    rows = []
    for item in feedback.get("diagnostics", ()):
        grade = item.get("diagnostic", {})
        row = {
            "case_id": item.get("case_id"),
            "question_id": grade.get("question_id"),
            "status": grade.get("status"),
            "precise": grade.get("precise"),
            "lenient": grade.get("lenient"),
        }
        if item.get("trace"):
            row["trace"] = item["trace"]
        rows.append(row)
    return {
        "scores": safe_scores(feedback["scores"]),
        "pipeline_active_stages": feedback.get("pipeline_active_stages"),
        "diagnostics": rows,
        "diagnostic_rows_total": feedback.get("diagnostic_rows_total", len(rows)),
        "diagnostic_rows_in_proposal": len(rows),
        "generation_failures": [
            {k: row.get(k) for k in ("case_id", "question_id", "error")}
            for row in feedback.get("generation_failures", ())
        ],
        "generation_failures_total": feedback.get("generation_failures_total", 0),
        "generation_failures_truncated": feedback.get("generation_failures_truncated", False),
    }


def asset_evidence(base, candidate=None):
    """Optimization-only code/contract evidence; never injected into answering."""
    before = {a.id: a for a in base.assets.assets} if base is not None else {}
    current = candidate or base
    rows = []
    for asset in current.assets.assets:
        old = before.get(asset.id)
        if candidate is not None and old is not None and old.fingerprint == asset.fingerprint:
            continue

        def view(a):
            if a is None:
                return None
            result = a.to_dict()
            result["fingerprint"] = a.fingerprint
            if len(result["content"]) > 4000:
                result["content"] = result["content"][:4000]
                result["content_truncated"] = True
            return result

        rows.append(
            {"asset_id": asset.id, "before": view(old) if candidate else None, "after": view(asset)}
        )
    return {
        "base_version": base.version if base else None,
        "candidate_version": current.version,
        "asset_changes": rows,
    }


def _compress_training_evidence(facts, budget=35000):
    """单训练例证据的字段级预算压缩（二次复查 P1：真实字段是 generated_answer 与
    training_examples[].trace[] 内的 rows/candidate_summary，不是顶层 answer/rows）。
    按序列化总预算逐级收紧；永久保留问题 ID、参数、失败签名（issues/error）、类型
    事实（json_type）、步数/预算事实、检查 ID 与来源指针计数。确定性、请求发出前。"""
    import json as _json

    def size():
        return len(_json.dumps(facts, ensure_ascii=False, default=str))

    def cap(obj, key, limit):
        value = obj.get(key)
        if isinstance(value, str) and len(value) > limit:
            obj[key] = value[:limit]
            obj[key + "_truncated"] = True

    def shrink_list(obj, key, keep):
        rows = obj.get(key)
        if isinstance(rows, list) and len(rows) > keep:
            obj[key + "_total"] = len(rows)
            obj[key] = rows[:keep]

    def shrink_blocks(obj, key, keep, chars):
        blocks = obj.get(key)
        if isinstance(blocks, list) and blocks:
            kept = []
            for block in blocks[:keep]:
                if isinstance(block, dict):
                    block = dict(block)
                    cap(block, "text", chars)
                kept.append(block)
            if len(blocks) > keep:
                obj[key + "_total"] = len(blocks)
            obj[key] = kept

    def each_example():
        for example in facts.get("training_examples", []) or []:
            if isinstance(example, dict):
                yield example

    def each_trace_event(example):
        for event in example.get("trace", []) or []:
            if isinstance(event, dict):
                yield event

    # Tier 1：长文本与行集（真实字段形态）
    for example in each_example():
        cap(example, "generated_answer", 600)
        cap(example, "baseline_answer", 200)
        cap(example, "answer", 600)
        shrink_blocks(example, "source_text", 2, 400)
        shrink_list(example, "rows", 1)
        shrink_list(example, "node_ids", 4)
        for event in each_trace_event(example):
            shrink_list(event, "rows", 1)
            shrink_list(event, "node_ids", 4)
            summary = event.get("candidate_summary")
            if isinstance(summary, dict):
                cap(summary, "answer", 300)
                cap(summary, "json_type", 40)
    trace = facts.get("trace")
    if isinstance(trace, list) and len(trace) > 6:
        facts["trace_total"] = len(trace)
        facts["trace"] = trace[-6:]
        facts["trace_truncated"] = True
    for change in facts.get("asset_changes", []):
        for side in ("before", "after"):
            asset = change.get(side) if isinstance(change, dict) else None
            if isinstance(asset, dict):
                cap(asset, "content", 1200)
    if size() <= budget:
        return

    # Tier 2：事件级收缩——summary/feedback 截短，行集全撤（保计数），保失败事件
    # 与其前一次调用的骨架（参数/检查ID/issues/json_type/步数/错误）。
    for example in each_example():
        events = example.get("trace")
        if isinstance(events, list) and len(events) > 4:
            kept, dropped = [], 0
            for event in events:
                if isinstance(event, dict) and (
                    event.get("stage") in ("tool_error", "candidate", "execution_error")
                    or str(event.get("error_type") or event.get("error") or "") != ""
                ):
                    kept.append(event)
                else:
                    dropped += 1
            if dropped and len(kept) >= 1:
                example["trace_events_dropped"] = dropped
                example["trace"] = kept[-4:]
        shrink_blocks(example, "source_text", 1, 250)
        cap(example, "generated_answer", 300)
        cap(example, "answer", 300)
        for event in each_trace_event(example):
            cap(event, "summary", 300)
            cap(event, "feedback", 150)
            if isinstance(event.get("rows"), list):
                event["rows_total"] = len(event["rows"])
                event.pop("rows", None)
            summary = event.get("candidate_summary")
            if isinstance(summary, dict):
                cap(summary, "answer", 150)
                for extra in ("summary", "note"):
                    summary.pop(extra, None)
    for change in facts.get("asset_changes", []):
        for side in ("before", "after"):
            asset = change.get(side) if isinstance(change, dict) else None
            if isinstance(asset, dict):
                cap(asset, "content", 600)
    if size() <= budget:
        return

    # Tier 3：仅剩事实骨架——保问题ID/参数/签名/类型/步数/指针计数与嵌套检查事实
    # （三次复查 P1：candidate 事件的 checks[].check_id/ok/issues/步数、
    # candidate_summary.json_type、observation 的预算事实不得丢），原文与摘要全撤。
    CORE_EVENT_KEYS = (
        "stage",
        "error",
        "error_type",
        "asset_id",
        "parameters",
        "attempt",
        "step",
        "action",
        "issues",
        "check_id",
        "json_type",
        "steps_used",
        "step_budget",
        "rows_total",
        "node_ids_total",
    )

    def _slim_event(event):
        slim = {k: event[k] for k in CORE_EVENT_KEYS if k in event}
        checks = event.get("checks")
        if isinstance(checks, list):
            slim["checks"] = [
                {k: c.get(k) for k in ("check_id", "ok", "issues", "steps_used", "step_budget")}
                for c in checks
                if isinstance(c, dict)
            ][:4]
        summary = event.get("candidate_summary")
        if isinstance(summary, dict):
            slim["candidate_summary"] = {
                k: summary[k] for k in ("json_type", "issues") if k in summary
            }
        observation = event.get("observation")
        if isinstance(observation, dict):
            slim["observation"] = {
                k: observation[k] for k in ("steps_used", "step_budget") if k in observation
            }
        return slim

    for example in each_example():
        example.pop("source_text", None)
        cap(example, "generated_answer", 160)
        cap(example, "answer", 160)
        events = example.get("trace")
        if isinstance(events, list):
            example["trace"] = [_slim_event(event) for event in events if isinstance(event, dict)]
        summary = example.get("candidate_summary")
        if isinstance(summary, dict):
            example["candidate_summary"] = {
                k: summary[k] for k in ("json_type", "issues") if k in summary
            }
    for change in facts.get("asset_changes", []):
        for side in ("before", "after"):
            asset = change.get(side) if isinstance(change, dict) else None
            if isinstance(asset, dict):
                cap(asset, "content", 300)


def bounded_trace(trace, limit):
    """Keep a late fault and its preceding call before filling chronological context."""
    indices = set()
    for i, event in enumerate(trace):
        if event.get("stage") == "tool_error" or any(
            not check.get("ok", True) for check in event.get("checks", [])
        ):
            indices.add(i)
            preceding = next(
                (j for j in range(i - 1, -1, -1) if trace[j].get("stage") == "tool"), None
            )
            if preceding is not None:
                indices.add(preceding)
    selected = sorted(indices)[:limit]
    selected.extend(i for i in range(len(trace)) if i not in indices)
    return [trace[i] for i in sorted(selected[:limit])]
