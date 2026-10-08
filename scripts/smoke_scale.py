"""Expanded real smoke: 24 new cases, 72 questions and six dynamic Wiki sessions."""

import argparse
import asyncio
import json
from pathlib import Path

import smoke_extended as extended
import smoke_scale_cases as scale
import smoke_stress as stress

from darwinagent.runtime.artifacts import atomic_json

WIKI_CASES = {
    "scale-page-21",
    "scale-page-41",
    "scale-page-101",
    "scale-out-of-range-reset",
    "scale-duplicate-technicians",
    "scale-interleaved-month-person",
}


def audit_page_execution(audit, definitions):
    questions = {(cid, qid): text for cid, _, qs in definitions for qid, text, _, _ in qs}
    rows = []
    for row in audit["rows"]:
        text = questions[(row["case_id"], row["question_id"])]
        if "按next_offset" not in text:
            continue
        calls = row["tools"]
        pages = [c for c in calls if c["asset_id"] == "f_page_facts"]
        expected_offset = 999 if "offset=999" in text else 0
        faults = []
        if not pages:
            faults.append("requested_paging_not_executed")
        for index, page in enumerate(pages):
            params, meta = page["parameters"], page["control"]
            if params.get("offset", 0) != expected_offset or params.get("limit", 20) != 20:
                faults.append(f"page_{index}_parameters")
            if "more_remain" not in meta or "next_offset" not in meta:
                faults.append(f"page_{index}_missing_control")
            if expected_offset == 999:
                expected_offset = 0
            else:
                expected_offset = meta.get("next_offset")
        if pages and pages[-1]["control"].get("more_remain") is not False:
            faults.append("final_page_not_finished")
        if "offset=999" in text and len(pages) < 2:
            faults.append("out_of_range_not_reset")
        rows.append(
            {
                "case_id": row["case_id"],
                "question_id": row["question_id"],
                "field_passed": row["passed"],
                "page_calls": len(pages),
                "faults": faults,
            }
        )
    return {"checked": len(rows), "passed": sum(not r["faults"] for r in rows), "rows": rows}


async def live(root, model, active_seconds=5400, max_requests=640):
    if model != "glm-5.3-flash":
        raise ValueError("Only the user-specified glm-5.3-flash is authorized")
    root = Path(root)
    definitions = scale.definitions()
    scale.preflight(root)
    generated = await extended.live(
        root,
        model,
        active_seconds,
        max_requests,
        definitions=definitions,
        concurrency=2,
        tool_steps=16,
        retry_interrupted=True,
    )
    audit = stress.audit_saved_answers(root, definitions)
    paging = audit_page_execution(audit, definitions)
    atomic_json(root / "page-execution-audit.json", paging)
    if audit["missing"]:
        wiki = {"status": "pending", "reason": "Missing answers; no adjacent generation from Wiki"}
    else:
        wiki = await stress.wiki_checks(
            root,
            model,
            active_seconds,
            max_requests,
            case_definitions=definitions,
            wiki_case_ids=WIKI_CASES,
            concurrency=2,
            identity="scale-24-72-v1",
            tool_steps=16,
        )
    report = {
        "requested_model": model,
        "concurrency": 2,
        "new_cases": len(definitions),
        "new_questions": audit["expected_questions"],
        "completed": audit["completed_questions"],
        "field_passed": audit["passed"],
        "missing": audit["missing"],
        "generation": generated["status"],
        "paging": paging,
        "wiki": wiki,
        "http_attempts": wiki.get("http_attempts", generated.get("http_attempts")),
        "transport_peak": generated.get("transport_peak"),
        "status": "complete"
        if generated["status"] == wiki["status"] == "complete"
        and paging["checked"] == paging["passed"]
        else "pending",
    }
    atomic_json(root / "scale-report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--active-seconds", type=int, default=5400)
    parser.add_argument("--max-requests", type=int, default=640)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(live(args.output, args.model, args.active_seconds, args.max_requests)),
            ensure_ascii=False,
            indent=2,
        )
    )
