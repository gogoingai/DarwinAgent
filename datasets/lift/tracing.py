"""Native cumulative roots, incremental sends/leaves and actual timestamps via ingestion API."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json, digest


def stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def push(trace_id, name, session, run_id, metadata, output, started, records, leaves):
    # The public ingestion API accepts recorded timestamps and deterministic ids.
    # SDK v4 start_observation does not accept start_time; using it after execution
    # would falsely measure only emission latency. No private SDK fields are used.
    from langfuse import get_client
    from langfuse.api.ingestion.types.ingestion_event import IngestionEvent
    from pydantic import TypeAdapter

    now = stamp(time.time())
    events = []

    def event(kind, body):
        events.append(
            TypeAdapter(IngestionEvent).validate_python(
                {
                    "id": digest({"trace": trace_id, "kind": kind, "body": body})[:32],
                    "type": kind,
                    "timestamp": now,
                    "body": body,
                }
            )
        )

    event(
        "trace-create",
        {
            "id": trace_id,
            "name": name,
            "timestamp": stamp(started),
            "sessionId": session,
            "tags": [run_id, session],
            "metadata": metadata,
            "output": output,
        },
    )
    event(
        "span-create",
        {
            "id": digest(trace_id + "root")[:16],
            "traceId": trace_id,
            "name": name,
            "startTime": stamp(started),
            "endTime": now,
            "metadata": metadata,
            "output": output,
        },
    )
    for record in records:
        usage = record["usage"] or {}
        details = {
            b: usage[a]
            for a, b in [("prompt_tokens", "input"), ("completion_tokens", "output")]
            if a in usage
        }
        event(
            "generation-create",
            {
                "id": record["id"][:16],
                "traceId": trace_id,
                "parentObservationId": digest(trace_id + "root")[:16],
                "name": "llm.chat",
                "model": record["model"],
                "startTime": stamp(record["started"]),
                "endTime": stamp(record["finished"]),
                "metadata": {
                    "role": record["role"],
                    "status": record["status"],
                    "actual_http_attempts": record["actual_http_attempts"],
                },
                "output": record.get("output"),
                "usageDetails": details or None,
            },
        )
    for leaf in leaves:
        event(
            "span-create",
            {
                "id": leaf["id"][:16],
                "traceId": trace_id,
                "parentObservationId": digest(trace_id + "root")[:16],
                "metadata": {"kind": "tool"},
                "name": leaf["name"],
                "input": leaf["params"],
                "output": leaf.get("output"),
                "startTime": stamp(leaf["started"]),
                "endTime": stamp(leaf["finished"]),
            },
        )
    result = get_client().api.ingestion.batch(batch=events)
    if result.errors:
        raise RuntimeError("Langfuse ingestion rejected observations")


def emit(root, session, run_id, role, history, episodes, shared, enabled, started):
    root = Path(root)
    records = [json.loads(p.read_text()) for p in sorted((shared.root / "requests").glob("*.json"))]
    records = [r for r in records if r["session"] == session and r["status"] != "budget_exhausted"]
    events = [e for result in episodes for e in result["events"]]
    leaves = [e for e in events if e["kind"] in {"tool", "tool_failure"}]
    calls = [
        {"type": "toolCall", "id": str(i), "name": e["name"], "arguments": e.get("params", {})}
        for i, e in enumerate(leaves)
    ]
    metadata = {
        "messages": history + [{"role": "assistant", "content": calls}],
        "toolCallBlocks": len(calls),
        "message_count": len(history),
        "role": role,
    }
    path = root / "trace-state.json"
    prior = (
        json.loads(path.read_text())
        if path.exists()
        else {"requests": [], "leaves": 0, "tool_receipts": []}
    )
    new_records = [r for r in records if r["id"] not in prior["requests"]]
    tool_receipts = [
        json.loads(p.read_text()) for p in sorted((root / "tool-timings").glob("*.json"))
    ]
    new_leaves = [r for r in tool_receipts if r["id"] not in prior.get("tool_receipts", [])]
    receipt = {
        "session": session,
        "metadata": metadata,
        "output": {"content": history[-1]["content"] if history else "", "tool_calls": calls},
        "new_model_sends": len(new_records),
        "new_leaf_executions": len(new_leaves),
        "tokens_known": all(
            r["usage"] and "prompt_tokens" in r["usage"] and "completion_tokens" in r["usage"]
            for r in new_records
        ),
    }
    atomic_json(root / "traces" / (digest(receipt) + ".json"), receipt)
    if enabled and (new_records or new_leaves):
        push(
            digest({"session": session, "history": history})[:32],
            "darwinagent-plugin",
            session,
            run_id,
            metadata,
            receipt["output"],
            started,
            new_records,
            new_leaves,
        )
    atomic_json(
        path,
        {
            "requests": [r["id"] for r in records],
            "leaves": len(leaves),
            "tool_receipts": [r["id"] for r in tool_receipts],
        },
    )
    return receipt


def emit_model_role(shared, record):
    """Proposal/Wiki costs have separate roots and never enter worker histories."""
    if record["status"] == "budget_exhausted":
        return
    push(
        digest(record["id"])[:32],
        "darwinagent-evolution",
        record["session"],
        shared.run_id,
        {"role": record["role"]},
        record.get("output"),
        record["started"],
        [record],
        [],
    )
