"""Training-only optimization memory. Facts are durable before model attribution."""

from __future__ import annotations

import json
import re
import inspect
from collections import Counter
from pathlib import Path

from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import DATA_CAPABILITIES, BUILTINS
from darwinagent.agents.protocol import ModelSession
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.contracts import freeze


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


def failure_patterns(facts):
    admission = facts.get("admission") or facts
    failures = [
        row
        for row in admission.get("scenarios", ())
        if row.get("required", True) and row.get("status") not in ("passed", "skipped")
    ]
    patterns = []
    for row in failures:
        error = re.sub(r"\b\d+\b", "#", str(row.get("error", "")))
        patterns.append(
            f"{row.get('asset_id')}:{row.get('scenario_id')}:{row.get('error_type')}:{error[:240]}"
        )
    if not patterns and facts.get("status") == "failed":
        error = re.sub(r"/(?:[^ ;:)]+/)*[^ ;:)]+", "<path>", str(facts.get("error", "")))
        patterns = [error[:300]]
    return sorted(set(patterns))


def _pattern_origins(facts):
    """与 failure_patterns 同构，但带回首个失败行（origin 绑定用，二次复查 P2：
    verified_fix 必须绑定原 case/参数/快照 digest——同名场景不同输入不算复现；
    四次复查反例 B 增记数据图摘要 graph_digests，供验证时图身份比对）。"""
    admission = facts.get("admission") or facts
    origins = {}
    for row in admission.get("scenarios", ()):
        if row.get("required", True) and row.get("status") not in ("passed", "skipped"):
            error = re.sub(r"\b\d+\b", "#", str(row.get("error", "")))
            pattern = f"{row.get('asset_id')}:{row.get('scenario_id')}:{row.get('error_type')}:{error[:240]}"
            origins.setdefault(
                pattern,
                {
                    "input_ref": row.get("input_ref", ""),
                    "error_type": row.get("error_type"),
                    "error": row.get("error", ""),
                    "issues": list(row.get("issues") or ()),
                    "graph_digests": dict(admission.get("graph_digests") or {}),
                },
            )
    if not origins and facts.get("status") == "failed":
        error = re.sub(r"\b/(?:[^ ;:)]+/)*[^ ;:)]+", "<path>", str(facts.get("error", "")))
        origins[error[:300]] = {
            "input_ref": "",
            "error_type": None,
            "error": str(facts.get("error", "")),
            "issues": [],
        }
    return origins


def _lessons(entries):
    lessons = {}
    for entry in entries:
        for pattern, origin in _pattern_origins(entry["facts"]).items():
            key = digest(pattern)
            lesson = lessons.setdefault(
                key,
                {
                    "id": key,
                    "pattern": pattern,
                    "stage": entry["stage"],
                    "status": "unresolved",
                    "confidence": "hypothesis",
                    "evidence_ids": [],
                    "origin": origin,
                    "cause": None,
                    "action": None,
                    "occurrences": 0,
                },
            )
            lesson["occurrences"] += 1
            lesson["evidence_ids"] = (lesson["evidence_ids"] + [entry["id"]])[-8:]
            lesson["last_stage"] = entry["stage"]
            lesson["status"] = "unresolved"
            if entry.get("attribution"):
                lesson.update(
                    cause=entry["attribution"]["cause"], action=entry["attribution"]["action"]
                )
        if entry["kind"] in ("formal", "decision") and entry.get("attribution"):
            attribution = entry["attribution"]
            lessons[entry["id"]] = {
                "id": entry["id"],
                "stage": entry["stage"],
                "last_stage": entry["stage"],
                "status": "strategy_hypothesis",
                "confidence": "hypothesis",
                "evidence_ids": [entry["id"]],
                "cause": attribution["cause"],
                "action": attribution["action"],
                "training_ids": attribution["training_ids"],
                "accepted": entry["facts"].get("accepted"),
                "scores": entry["facts"].get("scores"),
                "validation_scope": entry["scope"],
            }
        if entry["kind"] in ("formal", "decision") and entry.get("attribution"):
            attribution = entry["attribution"]
            lessons[entry["id"]] = {
                "id": entry["id"],
                "stage": entry["stage"],
                "last_stage": entry["stage"],
                "status": "strategy_hypothesis",
                "confidence": "hypothesis",
                "evidence_ids": [entry["id"]],
                "cause": attribution["cause"],
                "action": attribution["action"],
                "training_ids": attribution["training_ids"],
                "accepted": entry["facts"].get("accepted"),
                "scores": entry["facts"].get("scores"),
                "validation_scope": entry["scope"],
            }
        facts = entry["facts"]
        if (
            facts.get("status") == "passed"
            and (facts.get("verification") or {}).get("verdict") == "passed"
        ):
            report = facts["verification"]
            replay_proofs = [
                {
                    "scenario_id": s.get("scenario_id"),
                    "ref": s.get("input_ref"),
                    "check_ids": ([s["asset_id"]] if s.get("asset_id") else [])
                    + list(s.get("check_ids") or ()),
                    "expectation": s.get("expectation"),
                    "status": s.get("status"),
                }
                for s in report.get("scenarios", ())
                if str(s.get("scenario_id", "")).startswith("check_replay")
                and s.get("status") == "passed"
                and s.get("required", True)
            ]
            for change in facts.get("asset_changes", []):
                asset = change.get("after") or {}
                aid = asset.get("id", "__missing__")

                def _ref_digest(ref):
                    # input_ref 尾段是参数/快照 digest（case:tag:<hex>）；任务固定
                    # 负例（answer_invalid/answer_cx_*）无 digest 段。
                    tail = str(ref or "").rsplit(":", 1)[-1]
                    return (
                        tail
                        if len(tail) >= 12 and all(c in "0123456789abcdef" for c in tail)
                        else ""
                    )

                def _ref_identity(ref, scenario, graphs):
                    """Use report case IDs, which may themselves contain colons.

                    For old reports without graph identities, split at the known scenario;
                    check replay references instead end in question_id:snapshot_digest.
                    """
                    ref = str(ref or "")
                    for case_id in sorted(graphs, key=lambda c: (-len(c), c)):
                        if ref.startswith(case_id + ":"):
                            return case_id, ref[len(case_id) + 1 :].split(":", 1)[0]
                    marker = ":" + str(scenario)
                    at = ref.find(marker)
                    if at >= 0 and (at + len(marker) == len(ref) or ref[at + len(marker)] == ":"):
                        return ref[:at], str(scenario)
                    if _ref_digest(ref):
                        parts = ref.rsplit(":", 2)
                        if len(parts) == 3:
                            return parts[0], parts[1]
                    parts = ref.split(":", 1)
                    return parts[0], parts[1].split(":", 1)[0] if len(parts) > 1 else ""

                def _verifies(row, lesson, aid, report):
                    """四审五要素绑定：case＋场景族＋参数/快照 digest＋数据图身份＋期望档。
                    反例（recheck4 remaining-probes）：跨 case 同参数通过（base/stress 参数
                    来自资产级 trial_inputs，跨 case 必然同 digest）、同 case 跨场景同
                    digest、数据图已变（可重建图模式）都不得判「旧故障已修复」。证据
                    不足只算候选通过（外层 any 不命中→lesson 保持 unresolved）。
                    origin 无案例段（空 ref 的旧 lesson）时无案例可绑，退回场景身份。"""
                    if (
                        row.get("asset_id") != aid
                        or row.get("status") != "passed"
                        or not row.get("required", True)
                    ):
                        return False
                    if row.get("expectation") == "structure":
                        return False  # 占位实例结构档不能证明语义故障已修复
                    origin = lesson.get("origin") or {}
                    origin_ref = str(origin.get("input_ref") or "")
                    origin_digest = _ref_digest(origin_ref)
                    origin_scenario = (
                        lesson["pattern"].split(":")[1] if ":" in lesson["pattern"] else ""
                    )
                    origin_graphs = origin.get("graph_digests") or {}
                    report_graphs = report.get("graph_digests") or {}
                    origin_case, origin_slot = _ref_identity(
                        origin_ref, origin_scenario, origin_graphs
                    )
                    row_case, row_slot = _ref_identity(
                        row.get("input_ref"), row.get("scenario_id"), report_graphs
                    )
                    if origin_case and origin_case != row_case:
                        return False
                    if origin_digest:
                        # 数据相关场景：场景族一致＋原输入 digest 成功才算修复——
                        # 同 case 不同场景（stress 失败、base 通过）与同名不同参数
                        # （A→B 失败、只证 A→C）都不算（三次复查 P2 场景维度为四审
                        # 补全）。场景比对用 ref 段对 ref 段：check_replay 的 ref 是
                        # case:question_id:<digest>，第二段是题号而非场景名——与行
                        # scenario_id 标签不同源，比对标签会误拒真实回放验证。
                        if origin_slot != row_slot:
                            return False
                        if origin_digest != _ref_digest(row.get("input_ref")):
                            return False
                    else:
                        if row.get("scenario_id") != origin_scenario:
                            return False  # 静态/固定负例：场景身份
                    # 数据图身份：失败时的图与验证时的图不一致（可重建图模式）则
                    # 输入语义已变，不能证明原故障修复；任一侧缺失时退回前四要素
                    # （旧报告无图摘要，不追溯作废）。
                    origin_graph = origin_graphs.get(origin_case) if origin_case else None
                    if (
                        origin_graph
                        and origin_case in report_graphs
                        and report_graphs[origin_case] != origin_graph
                    ):
                        return False
                    return True

                for lesson in lessons.values():
                    if not lesson.get("pattern", "").startswith(aid + ":"):
                        continue
                    # 资产过门 ≠ 该故障已修复。绑定见 _verifies；无足够输入证据
                    # （原输入未在同 case 同场景成功重放）时保持 unresolved/hypothesis。
                    if not any(
                        _verifies(s, lesson, aid, report) for s in report.get("scenarios", ())
                    ):
                        continue
                    lesson.update(
                        status="admission_verified",
                        verification_event=entry["id"],
                        validation_scope=entry["scope"],
                        verified_asset_fingerprint=asset.get("fingerprint"),
                    )
                    lesson["verified_fix"] = {
                        k: asset.get(k)
                        for k in ("id", "kind", "input_contract", "output_contract", "trial_inputs")
                    }
                    lesson["verified_fix"]["content"] = asset.get("content", "")[:2000]
                    lesson["verified_fix"]["content_truncated"] = (
                        len(asset.get("content", "")) > 2000
                    )
                    proofs = [p for p in replay_proofs if aid in p["check_ids"]]
                    if proofs:
                        lesson["verified_fix"]["reproduced_checks"] = proofs
    return list(lessons.values())


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


def _brief_facts(facts):
    if not isinstance(facts, dict):
        return facts
    result = json.loads(json.dumps(facts, ensure_ascii=False))
    if isinstance(result.get("scenarios"), list):
        scenarios = result["scenarios"]
        result["scenario_count"] = len(scenarios)
        result["scenarios"] = [
            {
                key: row.get(key)
                for key in (
                    "asset_id",
                    "asset_fingerprint",
                    "scenario_id",
                    "status",
                    "error",
                    "error_type",
                    "ok",
                    "issues",
                    "expectation",
                    "check_id",
                    "json_type",
                    "input_ref",
                    "parameters",
                    "steps_used",
                    "step_budget",
                    "capability_calls",
                    "traverse_observations",
                )
            }
            for row in scenarios
            if row.get("required", True) and row.get("status") not in ("passed", "skipped")
        ][:8]
        result["verified_scenarios"] = dict(
            Counter(row.get("asset_id") for row in scenarios if row.get("status") == "passed")
        )
    if isinstance(result.get("diagnostics"), list):
        rows = result["diagnostics"]
        result["diagnostics_total"] = len(rows)
        result["diagnostics"] = rows[:6]
    for key in ("admission", "verification"):
        if isinstance(result.get(key), dict):
            result[key] = _brief_facts(result[key])
    for patch in result.get("patches", []):
        asset = patch.get("asset")
        if asset and len(asset.get("content", "")) > 3000:
            asset["content"] = asset["content"][:3000]
            asset["content_truncated"] = True
    if isinstance(result.get("scenarios"), list):
        for scenario in result["scenarios"]:
            params = scenario.get("parameters")
            if isinstance(params, dict):
                for key, value in list(params.items()):
                    if isinstance(value, list) and len(value) > 5:
                        params[key] = value[:5]
                        scenario["parameters_truncated"] = True
    return result


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


def _formal_runtime_facts(facts):
    scenarios = []
    for example in facts.get("training_examples", []):
        errors = [e for e in example.get("trace", []) if e.get("stage") == "tool_error"]
        checks = [
            c
            for event in example.get("trace", [])
            for c in event.get("checks", [])
            if c.get("ok") is False
        ]
        for check in checks:
            scenarios.append(
                {
                    "asset_id": check.get("check_id"),
                    "asset_fingerprint": check.get("fingerprint"),
                    "scenario_id": "formal_candidate_check",
                    "status": "failed",
                    "required": True,
                    "training_id": example["training_id"],
                    "parameters": example.get("question_parameters"),
                    "error": "; ".join(check.get("issues", [])),
                    "error_type": "CandidateCheckRejected",
                    "candidate_json_type": next(
                        (
                            event.get("candidate_summary", {}).get("json_type")
                            for event in example.get("trace", [])
                            if check in event.get("checks", [])
                        ),
                        None,
                    ),
                    "steps_used": check.get("steps_used"),
                    "final_answer_status": example.get("status"),
                }
            )
        if not errors and not checks and example.get("status") == "execution_error":
            errors = [{"error": example.get("error"), "asset_id": "__generation__"}]
        for error in errors:
            observation = error.get("observation") or {}
            scenarios.append(
                {
                    "asset_id": error.get("asset_id", "__generation__"),
                    "scenario_id": "formal_tool_call",
                    "status": "failed",
                    "required": True,
                    "training_id": example["training_id"],
                    "parameters": error.get("parameters"),
                    "error": error.get("error") or error.get("summary"),
                    "error_type": error.get("error_type"),
                    "steps_used": observation.get("steps_used"),
                    "capability_calls": error.get("capability_calls"),
                    "traverse_observations": observation.get("traverse_observations"),
                    "final_answer_status": example.get("status"),
                }
            )
    return (
        {
            "status": "failed",
            "candidate_version": facts.get("candidate_version"),
            "scenarios": scenarios,
        }
        if scenarios
        else None
    )


def _context_facts(facts):
    result = _brief_facts(facts)
    examples = result.get("training_examples", [])
    result["training_examples"] = examples[:3] if examples else []
    if len(examples) > 3:
        result["training_examples_truncated"] = True
    for example in result["training_examples"]:
        example["trace"] = [
            {
                "stage": event.get("stage"),
                "summary": json.dumps(event, ensure_ascii=False)[:1000],
                "truncated": len(json.dumps(event, ensure_ascii=False)) > 1000,
            }
            for event in bounded_trace(example.get("trace", []), 4)
        ]
        for source in example.get("source_text", []):
            source["text"] = source["text"][:450]
        example["source_text"] = example.get("source_text", [])[:3]
    for change in result.get("asset_changes", []):
        if change.get("before"):
            change["before"] = {k: change["before"].get(k) for k in ("id", "fingerprint")}
        asset = change.get("after")
        if asset and len(asset.get("content", "")) > 1200:
            asset["content"] = asset["content"][:1200]
            asset["content_truncated"] = True
    return result


class WikiMaintainer:
    def __init__(self, root, identity, client_factory, config, limit=30):
        self.root = Path(root) / "optimization"
        self.identity = identity
        self.client_factory = client_factory
        self.config = config
        self.limit = limit
        self.wiki_path = self.root / "wiki.json"
        self.state_path = self.root / "state.json"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state["identity"] != identity or state["limit"] != limit:
                raise ValueError("Wiki optimization identity or budget mismatch")
        else:
            atomic_json(
                self.state_path, {"identity": identity, "limit": limit, "reserved_calls": 0}
            )
        if self.wiki_path.exists():
            if json.loads(self.wiki_path.read_text())["identity"] != identity:
                raise ValueError("Wiki identity mismatch")
        else:
            atomic_json(
                self.wiki_path,
                {"identity": identity, "version": 0, "consumed_ids": [], "entries": []},
            )

    def _wiki(self):
        return json.loads(self.wiki_path.read_text())

    def context(self, max_chars=18000):
        wiki = self._wiki()
        lessons = []
        for lesson in reversed(wiki.get("lessons", _lessons(wiki["entries"]))):
            if len(json.dumps([lesson, *lessons], ensure_ascii=False)) <= max_chars // 3:
                lessons.insert(0, lesson)
        selected = []
        # Latest formal/decision evidence is retained before repeated admission logs.
        ranked = sorted(
            enumerate(wiki["entries"]),
            key=lambda pair: (pair[1]["kind"] in ("formal", "decision"), pair[0]),
            reverse=True,
        )
        selected_ids = set()
        for index, entry in ranked:
            brief = {**entry, "facts": _context_facts(entry["facts"])}
            candidate = [(index, brief), *selected]
            payload = {
                "version": wiki["version"],
                "lessons": lessons,
                "entries": [e for _, e in candidate],
            }
            if len(json.dumps(payload, ensure_ascii=False)) <= max_chars:
                selected = candidate
                selected_ids.add(index)
        return {
            "version": wiki["version"],
            "lessons": lessons,
            "entries": [e for _, e in sorted(selected)],
            "truncated": len(selected_ids) < len(wiki["entries"]),
        }

    def new_failure(self, facts):
        patterns = set(failure_patterns(facts))
        known = set()
        for entry in self._wiki()["entries"]:
            if entry.get("attribution") or entry.get("pending_attribution"):
                known.update(failure_patterns(entry["facts"]))
        return bool(patterns - known)

    def reconcile(self):
        for path in sorted((self.root / "events").glob("*.json")):
            event = json.loads(path.read_text())
            self._consume(event)
            self._refresh(event["id"])

    def _consume(self, event):
        wiki = self._wiki()
        if event["id"] in wiki["consumed_ids"]:
            return
        attribution_path = self.root / "maintenance" / f"{event['id']}.json"
        attribution = (
            json.loads(attribution_path.read_text()) if attribution_path.exists() else None
        )
        entry = {
            k: event[k]
            for k in ("id", "stage", "kind", "category", "scope", "training_ids", "facts", "source")
        }
        entry["fact_status"] = "recorded"
        entry["confidence"] = "hypothesis"
        entry["validation_scope"] = event["scope"]
        entry["attribution"] = attribution.get("attribution") if attribution else None
        entry["pending_attribution"] = bool(event.get("infer") and not attribution)
        wiki["entries"].append(entry)
        wiki["consumed_ids"].append(event["id"])
        wiki["version"] += 1
        wiki["lessons"] = _lessons(wiki["entries"])
        atomic_json(self.wiki_path, wiki)

    async def record(
        self,
        stage,
        kind,
        facts,
        *,
        category="runtime",
        scope="trial",
        training_ids=(),
        source="",
        infer=False,
    ):
        event = {
            "stage": stage,
            "kind": kind,
            "facts": facts,
            "category": category,
            "scope": scope,
            "training_ids": list(training_ids),
            "source": source,
            "infer": infer,
        }
        event["id"] = digest({"identity": self.identity, **event})
        path = self.root / "events" / f"{event['id']}.json"
        if not path.exists():
            atomic_json(path, event)
        self._consume(event)
        if infer:
            await self._attribute(event)
        if kind == "formal":
            runtime_facts = _formal_runtime_facts(facts)
            if runtime_facts:
                # Mechanical failures are separate from score/strategy hypotheses.
                # Decision attribution still sees both; this adds no model call.
                await self.record(
                    stage,
                    "runtime_faults",
                    runtime_facts,
                    category="runtime",
                    scope=scope,
                    training_ids=training_ids,
                    source=source,
                )
        return event["id"]

    async def _attribute(self, event):
        target = self.root / "maintenance" / f"{event['id']}.json"
        if target.exists():
            return
        request_path = self.root / "maintenance" / f"{event['id']}.request.json"
        if request_path.exists():
            # A lost reply might already have consumed tokens. Never replay it silently.
            return
        state = json.loads(self.state_path.read_text())
        if state["reserved_calls"] >= self.limit:
            return
        remaining = self.limit - state["reserved_calls"]
        reserved = min(self.config.protocol_attempts, remaining)
        payload = {
            "event": {**event, "facts": _brief_facts(event["facts"])},
            "wiki_version": self._wiki()["version"],
            "recent": self.context(8000)["entries"][-4:],
            "runtime_contract": {
                "capabilities": {
                    name: str(
                        inspect.signature(getattr(DataCapabilities, name)).replace(
                            parameters=[
                                p
                                for p in inspect.signature(
                                    getattr(DataCapabilities, name)
                                ).parameters.values()
                                if p.name != "self"
                            ]
                        )
                    )
                    for name in sorted(DATA_CAPABILITIES)
                },
                "capability_implementations": {
                    name: inspect.getsource(getattr(DataCapabilities, name))
                    for name in ("nodes", "search", "traverse")
                },
                "result_shapes": {
                    "nodes": "rows[]",
                    "search": "rows[]",
                    "traverse": "rows[]",
                    "project": "rows[] (not column values)",
                    "relative_date": "object with resolved/precision",
                },
                "input_freeze_implementation": inspect.getsource(freeze),
                "builtin_names": sorted(BUILTINS),
                "asset_contract_types": {
                    "type": "single string only; union arrays are unsupported",
                    "allowed": [
                        "object",
                        "array",
                        "string",
                        "integer",
                        "number",
                        "boolean",
                        "null",
                        "any",
                    ],
                    "nullable": "normalize to declared scalar/object, or use type any; never type ['object','null']",
                },
                "candidate_boundary": "answer is text; structured_answer is the already parsed JSON value or null. C receives recursively frozen arrays (tuple) and objects (read-only mapping), so isinstance(x,list/dict) rejects valid inputs. Mandatory answer_contract checks are separate.",
                "function_steps": self.config.function_steps,
                "result_bytes": self.config.result_bytes,
            },
        }
        # Deterministic bounding preserves current evidence ahead of historical events.
        while len(json.dumps(payload, ensure_ascii=False)) > 35000 and payload["recent"]:
            payload["recent"].pop(0)
        facts = payload["event"]["facts"]
        while (
            len(json.dumps(payload, ensure_ascii=False)) > 35000
            and len(facts.get("training_examples", [])) > 1
        ):
            facts["training_examples"].pop()
            facts["training_examples_truncated"] = True
        if len(json.dumps(payload, ensure_ascii=False)) > 35000:
            for change in facts.get("asset_changes", []):
                for side in ("before", "after"):
                    asset = change.get(side)
                    if asset and len(asset.get("content", "")) > 1200:
                        asset["content"] = asset["content"][:1200]
                        asset["content_truncated"] = True
        if len(json.dumps(payload, ensure_ascii=False)) > 35000:
            # 字段级预算压缩（2026-10-05 审查 P1/二次复查 P1：超长即放弃导致 B0 formal
            # 等归因缺失）。预算按「完整 payload 减去其余部分」计——facts 压到该预算内
            # 才保证总请求 ≤35000。确定性、请求发出前：不重放模型调用，不扩大上限。
            rest = {k: v for k, v in payload.items() if k != "event"}
            rest["event"] = {k: v for k, v in payload["event"].items() if k != "facts"}
            rest_chars = len(json.dumps(rest, ensure_ascii=False, default=str))
            _compress_training_evidence(facts, budget=max(4000, 35000 - rest_chars))
        if len(json.dumps(payload, ensure_ascii=False)) > 35000:
            # Facts remain durable; an oversized attribution is a visible pending task.
            atomic_json(
                self.root / "maintenance" / f"{event['id']}.failure.json",
                {"error": "Maintenance evidence exceeds 35000 characters"},
            )
            return
        atomic_json(request_path, payload)
        state["reserved_calls"] += reserved
        atomic_json(self.state_path, state)
        client = self.client_factory("optimization")
        session = ModelSession(client, self.config, "wiki_maintenance", limit=reserved)

        def valid(value):
            if set(value) != {"cause", "action", "training_ids"}:
                raise ValueError("Invalid Wiki attribution fields")
            if not all(
                isinstance(value[k], str) and len(value[k]) <= 1200 for k in ("cause", "action")
            ):
                raise ValueError("Invalid Wiki attribution text")
            if not isinstance(value["training_ids"], list) or (
                set(value["training_ids"]) - set(event["training_ids"])
            ):
                raise ValueError("Wiki attribution cites non-training evidence")
            return value

        try:
            response = await session.request(
                "wiki_maintainer",
                "只归因给定训练事实和运行契约；区分候选代码错误、框架覆盖缺口与服务故障。"
                "生成答案不等于标准答案，不可把某题答案写成规则；联合修改不能证明单项贡献。"
                "原因先作为假设，修法须描述通用规则并引用给定训练 ID。禁止猜测标准答案或修改分数。仅返回 JSON "
                '{"cause":"原因或待验证假设","action":"后续修法","training_ids":[]}',
                payload,
                valid,
                max_tokens=1600,
            )
            atomic_json(
                target,
                {"attribution": response, "raw_outputs": session.raw, "events": session.events},
            )
        except Exception as exc:
            atomic_json(
                self.root / "maintenance" / f"{event['id']}.failure.json",
                {"error": f"{type(exc).__name__}: {exc}", "events": session.events},
            )
        finally:
            # Requests are reserved before the call. Unused slots return only after
            # an observable completion; a crash leaves the whole reservation charged.
            state = json.loads(self.state_path.read_text())
            state["reserved_calls"] -= reserved - session.calls
            atomic_json(self.state_path, state)
            await client.aclose()
            self._refresh(event["id"])

    def _refresh(self, event_id):
        target = self.root / "maintenance" / f"{event_id}.json"
        if not target.exists():
            return
        wiki = self._wiki()
        for entry in wiki["entries"]:
            if entry["id"] == event_id and entry["pending_attribution"]:
                entry["attribution"] = json.loads(target.read_text())["attribution"]
                entry["pending_attribution"] = False
                wiki["version"] += 1
                wiki["lessons"] = _lessons(wiki["entries"])
                atomic_json(self.wiki_path, wiki)
                break
