"""Pure projections of durable Wiki facts for model context."""

from __future__ import annotations

import json
from collections import Counter

from .wiki_evidence import bounded_trace, pagination_anomalies, pagination_metadata


def _brief_facts(facts):
    if not isinstance(facts, dict):
        return facts
    result = json.loads(json.dumps(facts, ensure_ascii=False))
    result.pop("_original_training_evidence", None)
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
    examples = sorted(
        examples,
        key=lambda e: any(pagination_anomalies(ev) for ev in e.get("trace", [])),
        reverse=True,
    )
    result["training_examples"] = examples[:3] if examples else []
    if len(examples) > 3:
        result["training_examples_truncated"] = True
    for example in result["training_examples"]:
        example["trace"] = [
            {
                "stage": event.get("stage"),
                "pagination": pagination_metadata(event),
                "anomaly_hints": pagination_anomalies(event),
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
