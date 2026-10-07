"""Failure patterns and evidence-bound repair lessons."""

from __future__ import annotations

import re

from darwinagent.runtime.artifacts import digest


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
