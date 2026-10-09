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


def session_totals(root):
    root = Path(root)
    # Each native turn has one durable slot, including the currently unfinished turn.
    turns = [json.loads(p.read_text()) for p in sorted((root / "turns").glob("*.json"))]
    if not turns and (root / "chat.json").exists():
        turns = json.loads((root / "chat.json").read_text())["turns"]
    episodes = [t["episode"] for t in turns if t.get("episode") is not None]
    usage = {
        k: sum(e["usage"].get(k, 0) for e in episodes)
        for k in ("model_calls", "leaf_calls", "unknown_calls", "pending_calls")
    }
    events = [v for e in episodes for v in e["events"] if v["kind"] == "model"]
    known = all(
        all(type(e.get("usage", {}).get(k)) is int for k in ("prompt_tokens", "completion_tokens"))
        for e in events
    )
    return {
        "usage": usage,
        "tokens": sum(
            sum(e["usage"][k] for k in ("prompt_tokens", "completion_tokens")) for e in events
        )
        if known
        else None,
        "time_s": sum(t.get("elapsed_s", 0) for t in turns),
        "turns": len(turns),
        "completed_turns": sum(e["status"] == "completed" for e in episodes),
        "unfinished_turns": sum(t.get("status") != "completed" for t in turns),
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
                k: sum(t["usage"][k] for t in totals)
                for k in ("model_calls", "leaf_calls", "unknown_calls", "pending_calls")
            }
            row[role + "_tokens"] = (
                sum(t["tokens"] for t in totals)
                if all(t["tokens"] is not None for t in totals)
                else None
            )
            row[role + "_time_s"] = sum(t["time_s"] for t in totals)
            row[role + "_turns"] = sum(t["turns"] for t in totals)
            row[role + "_unfinished_turns"] = sum(t["unfinished_turns"] for t in totals)
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
    value = {
        "status": "paused"
        if (shared.root / "pause.json").exists()
        else (
            "complete"
            if {
                r.get("arm")
                for r in rows
                if r.get("task_id") == "q5" and r["status"] == "completed"
            }
            >= {"A", "B", "C"}
            else "incomplete"
        ),
        "rows": rows,
        "all_pairs_retained": True,
        "roles": roles,
        "actual_http_attempts": shared.client.http_attempts(),
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
