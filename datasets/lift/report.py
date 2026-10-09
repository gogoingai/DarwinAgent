"""All-pair reporting retains regressions and unknown values instead of success filtering."""

import json
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json


def report(root, shared):
    root = Path(root)
    rows = [json.loads(p.read_text()) for p in sorted((root / "rows").glob("*.json"))]
    requests = [
        json.loads(p.read_text()) for p in sorted((shared.root / "requests").glob("*.json"))
    ]
    for path in sorted((root / "sessions").glob("*/last-episode.json")):
        episode = json.loads(path.read_text())
        if episode["status"] != "completed":
            identity = json.loads((path.parent / "session-identity.json").read_text())
            rows.append(
                {
                    **identity,
                    "status": episode["status"],
                    "native": None,
                    "worker_tokens": None,
                    "worker_usage": episode["usage"],
                    "trajectory_judge": "not_run",
                }
            )
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
            if {r.get("arm") for r in rows if r.get("task_id") == "q5"} >= {"A", "B", "C"}
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
