"""All-pair reporting retains regressions and unknown values instead of success filtering."""

import json
import time
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json, digest


def cell(root, task_id, arm, phase, run_id, **updates):
    key = digest({"task_id": task_id, "arm": arm, "phase": phase, "run_id": run_id})[:24]
    path = Path(root) / "cells" / (key + ".json")
    value = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "cell_id": key,
            "task_id": task_id,
            "arm": arm,
            "phase": phase,
            "run_id": run_id,
            "status": "unstarted",
            "sessions": [],
            "native": None,
            "trajectory_judge": "not_run",
        }
    )
    value.update(updates)
    atomic_json(path, value)
    return key


def update_cell(root, key, **updates):
    path = Path(root) / "cells" / (key + ".json")
    value = json.loads(path.read_text())
    value.update(updates)
    atomic_json(path, value)


USAGE_KEYS = ("model_calls", "leaf_calls", "unknown_calls", "pending_calls")


def available_sum(values):
    values = list(values)
    return sum(values) if all(v is not None for v in values) else None


def token_count(usage):
    keys = ("prompt_tokens", "completion_tokens")
    return (
        sum(usage[k] for k in keys)
        if all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys)
        else None
    )


def turn_totals(root, index, turn):
    episode = turn.get("episode")
    steps = [
        json.loads(p.read_text())
        for p in sorted((root / "episodes" / ("turn-" + str(index)) / "steps").glob("[0-9]*.json"))
    ]
    models = [s for s in steps if s["kind"] == "model"]
    events = [e for e in episode["events"] if e["kind"] == "model"] if episode else []
    if steps:
        usage = {
            "model_calls": len(models),
            "leaf_calls": sum(s["kind"] == "tool" for s in steps),
            "unknown_calls": sum(s["state"] == "submitted" for s in steps),
            "pending_calls": sum(s["state"] in {"prepared", "submitted"} for s in steps),
        }
        counts = [
            token_count(s.get("response", {}).get("usage", {}))
            if s["state"] == "responded"
            else None
            for s in models
        ]
        # A response may be durable before the Agent emits its model event. Keep
        # its measured subtotal, but do not call that an event-complete total.
        correspondence = len(models) == len(events) and all(
            s["state"] == "responded"
            and e.get("usage") == s.get("response", {}).get("usage")
            and e.get("response_digest") == digest(s.get("response", {}).get("content"))
            for s, e in zip(models, events)
        )
        if episode:
            correspondence = correspondence and episode["usage"].get("model_calls") == len(models)
    elif episode:
        usage = {k: episode["usage"].get(k) for k in USAGE_KEYS}
        counts = [token_count(e.get("usage", {})) for e in events]
        correspondence = usage["model_calls"] == len(events)
    else:
        # A turn was entered but neither result nor durable reservations exist.
        # Missing accounting is not evidence of zero dispatches.
        return {
            "usage": dict.fromkeys(USAGE_KEYS),
            "tokens": None,
            "known_tokens_subtotal": 0,
            "unknown_token_requests": None,
        }
    subtotal = sum(c for c in counts if c is not None)
    unknown = (
        max(0, usage["model_calls"] - sum(c is not None for c in counts))
        if usage["model_calls"] is not None
        else None
    )
    complete = episode is not None and correspondence and unknown == 0
    return {
        "usage": usage,
        "tokens": subtotal if complete else None,
        "known_tokens_subtotal": subtotal,
        "unknown_token_requests": unknown,
    }


def session_totals(root):
    root = Path(root)
    # Keep the durable index: lexicographic ordering must not remap turn 10 to 2.
    turns = [(p.stem, json.loads(p.read_text())) for p in sorted((root / "turns").glob("*.json"))]
    if not turns and (root / "chat.json").exists():
        turns = list(enumerate(json.loads((root / "chat.json").read_text())["turns"]))
    totals = [turn_totals(root, index, t) for index, t in turns]
    tokens = available_sum(t["tokens"] for t in totals)
    return {
        "usage": {k: available_sum(t["usage"][k] for t in totals) for k in USAGE_KEYS},
        "tokens": tokens,
        "known_tokens_subtotal": sum(t["known_tokens_subtotal"] for t in totals),
        "unknown_token_requests": available_sum(t["unknown_token_requests"] for t in totals),
        "accounting_status": "unstarted"
        if not turns
        else "complete"
        if tokens is not None
        else "incomplete",
        "time_s": sum(t.get("elapsed_s", 0) for _, t in turns),
        "turns": len(turns),
        "completed_turns": sum(
            t.get("episode", {}).get("status") == "completed" for _, t in turns if t.get("episode")
        ),
        "unfinished_turns": sum(
            t.get("status", (t.get("episode") or {}).get("status")) != "completed" for _, t in turns
        ),
    }


def cell_rows(root):
    rows = []
    for path in sorted((root / "cells").glob("*.json")):
        row = json.loads(path.read_text())
        if row["status"] == "running":
            row["status"] = "paused"
        row["task_time_s"] = (
            row.get("finished", time.time()) - row["started"] if "started" in row else None
        )
        for role in ("worker", "judge"):
            totals = [
                session_totals(root / "sessions" / sid)
                for sid in row["sessions"]
                if sid.startswith(role + "-")
            ]
            row[role + "_usage"] = {
                k: available_sum(t["usage"][k] for t in totals)
                for k in ("model_calls", "leaf_calls", "unknown_calls", "pending_calls")
            }
            row[role + "_tokens"] = available_sum(t["tokens"] for t in totals)
            row[role + "_known_tokens_subtotal"] = sum(t["known_tokens_subtotal"] for t in totals)
            row[role + "_unknown_token_requests"] = available_sum(
                t["unknown_token_requests"] for t in totals
            )
            row[role + "_accounting_status"] = (
                "unstarted"
                if not totals or all(t["accounting_status"] == "unstarted" for t in totals)
                else "incomplete"
                if any(t["accounting_status"] == "incomplete" for t in totals)
                else "complete"
            )
            row[role + "_time_s"] = sum(t["time_s"] for t in totals)
            row[role + "_turns"] = sum(t["turns"] for t in totals)
            row[role + "_unfinished_turns"] = sum(t["unfinished_turns"] for t in totals)
        if row["status"] == "failed":
            row["final_content_score"] = None
            row["last_intermediate_content_score"] = (row.get("native") or {}).get("content_score")
        else:
            row["final_content_score"] = (row.get("native") or {}).get("content_score")
        row["correction_turns"] = max(0, row["worker_turns"] - 1)
        row["native_tool_calls"] = row["flattened_leaf_calls"] = row["worker_usage"]["leaf_calls"]
        rows.append(row)
    return rows


def report(root, shared):
    root = Path(root)
    rows = cell_rows(root)
    if not rows:
        rows = [json.loads(p.read_text()) for p in sorted((root / "rows").glob("*.json"))]
    requests = [
        json.loads(p.read_text()) for p in sorted((shared.root / "requests").glob("*.json"))
    ]
    global_request_count = len(requests)
    requests = [r for r in requests if r.get("run_id") == shared.run_id]
    roles = {}
    for r in requests:
        value = roles.setdefault(
            r["role"], {"requests": 0, "tokens": 0, "unknown_token_requests": 0, "elapsed_s": 0.0}
        )
        value.setdefault("actual_http_attempts", 0)
        value["actual_http_attempts"] += r.get("actual_http_attempts", 0)
        if r["status"] == "budget_exhausted" and r.get("actual_http_attempts") == 0:
            value["denied_reservations"] = value.get("denied_reservations", 0) + 1
            continue
        value["requests"] += 1
        usage = r["usage"] or {}
        if not all(type(usage.get(k)) is int for k in ("prompt_tokens", "completion_tokens")):
            value["unknown_token_requests"] += 1
        else:
            value["tokens"] += usage["prompt_tokens"] + usage["completion_tokens"]
        value["elapsed_s"] += r.get("finished", r["started"]) - r["started"]
    for value in roles.values():
        if value["unknown_token_requests"]:
            value["known_tokens_subtotal"] = value.pop("tokens")
            value["tokens"] = None
    from darwinagent.llm.client import BudgetExceeded

    try:
        shared.check()
        paused = False
    except BudgetExceeded:
        paused = True
    value = {
        "status": "paused"
        if paused
        else (
            ("finished_with_failures" if any(r["status"] == "failed" for r in rows) else "complete")
            if {
                r.get("arm")
                for r in rows
                if r.get("task_id") == "q5" and r["status"] in {"completed", "failed"}
            }
            >= {"A", "B", "C"}
            else "incomplete"
        ),
        "rows": rows,
        "all_pairs_retained": True,
        "roles": roles,
        "actual_http_attempts": shared.client.http_attempts(),
        "http_cap": shared.cfg.max_http_requests,
        "remaining_http_attempts": max(
            0, shared.cfg.max_http_requests - shared.client.http_attempts()
        ),
        "global_request_records": global_request_count,
        "campaign_request_records": len(requests),
        "model_evidence": "recorded_offline" if not shared.cfg.api_base_url else "host_transport",
        "budget_root": str(shared.root.resolve()),
        "trajectory_judge": "not_run",
        "q6": "sealed_not_opened",
        "native_content_judge": True,
        "timing_scope": "worker_time_s sums measured ReAct turn durations; role elapsed_s covers model transport; null task_time_s means unavailable",
    }
    atomic_json(root / "all-pair-report.json", value)
    return value
