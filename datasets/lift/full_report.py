"""Physical full-matrix coverage, quality and measured cost reporting."""

import csv
import json

from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest

from .report import available_sum, token_count


def role_costs(requests):
    roles = {}
    for role in sorted({r.get("role", "unknown") for r in requests}):
        records = [r for r in requests if r.get("role", "unknown") == role]
        roles[role] = dict(
            requests=len(records),
            actual_http_attempts=available_sum(r.get("actual_http_attempts") for r in records),
            tokens=available_sum(token_count(r.get("usage") or {}) for r in records),
            elapsed_s=available_sum(
                r["finished"] - r["started"] if "finished" in r else None for r in records
            ),
        )
    return roles


def host_evidence(owner, required, current_requests):
    from darwinagent.llm.client import LLMClient

    try:
        owner.verify()
    except ValueError:
        return False
    cfg = owner.shared.cfg
    if (
        type(owner.shared.client) is not LLMClient
        or getattr(owner.shared.client, "provenance", "host_transport") != "host_transport"
        or cfg.api_base_url != "https://api.minimax.cn/v1"
        or cfg.model_strong != "MiniMax-M3.1-Flash-Preview"
        or cfg.provider_request_policy != "minimax-adaptive-v1"
    ):
        return False
    for row in required:
        native = row.get("native") or {}
        directory = owner.root / "episodes" / row.get("evidence_key", "missing")
        for role, session, journal in (
            ("worker", native.get("work_session_id"), directory / "worker" / "chat.json"),
            ("judge", native.get("judge_session_id"), directory / "judge" / "chat.json"),
        ):
            if not session or not journal.exists():
                return False
            events = [
                event
                for turn in json.loads(journal.read_text())["turns"]
                for event in turn["episode"]["events"]
                if event["kind"] == "model"
            ]
            records = [
                r for r in current_requests if r.get("session") == session and r.get("role") == role
            ]
            if not events or len(events) != len(records):
                return False
            records.sort(key=lambda r: r["started"])
            if any(
                r.get("status") != "completed"
                or not r.get("actual_http_attempts", 0)
                or r.get("model") != cfg.model_strong
                or digest(r.get("output")) != event.get("response_digest")
                or r.get("usage") != event.get("usage")
                for r, event in zip(records, events)
            ):
                return False
        trajectory = row.get("trajectory") or {}
        trajectory_session = (trajectory.get("identity") or {}).get("session")
        records = [
            r
            for r in current_requests
            if r.get("session") == trajectory_session and r.get("role") == "trajectory_judge"
        ]
        if (
            trajectory.get("status") != "completed"
            or len(records) != 1
            or records[0].get("status") != "completed"
            or not records[0].get("actual_http_attempts", 0)
            or records[0].get("output") != trajectory.get("raw_response")
        ):
            return False
    return bool(required)


def write_report(owner):
    rows = [json.loads(p.read_text()) for p in sorted((owner.root / "cells").glob("*.json"))]
    required = [r for r in rows if r["arm"] in ("A", "B", "C", "campaign")]
    holdout = [r for r in required if r["phase"] == "holdout"]
    warmup = [r for r in required if r["arm"] == "campaign"]
    required_count = 10 * len(owner.scenes)
    completed = sum(r["status"] in ("completed", "failed") for r in required)
    coverage = len(required) == required_count and completed == required_count
    healthy = coverage and all(r.get("healthy") for r in required)
    c = [r for r in holdout if r["arm"] == "C"]
    quality = len(c) == len(owner.scenes) * 2 and all(
        r.get("healthy")
        and (r.get("native") or {}).get("success") is True
        and (r.get("native") or {}).get("content_score") == 1
        for r in c
    )
    full = set(owner.scenes) == set(owner.dataset.scenes)
    requests = [json.loads(p.read_text()) for p in (owner.shared.root / "requests").glob("*.json")]
    current = [r for r in requests if r.get("run_id") == owner.run_id]
    verified_host = host_evidence(owner, required, current)
    costs = dict(
        roles=role_costs(current),
        global_roles=role_costs(requests),
        actual_http_attempts=owner.shared.client.http_attempts(),
        current_http_attempts=available_sum(r.get("actual_http_attempts") for r in current),
        model_evidence="host_transport"
        if verified_host
        else (
            "recorded_offline"
            if type(owner.shared.client).__name__ != "LLMClient"
            or getattr(owner.shared.client, "provenance", "host_transport") != "host_transport"
            else "unverified_host_evidence"
        ),
    )
    for row in rows:
        directory = owner.root / "episodes" / row.get("evidence_key", "missing")
        for role in ("worker", "judge"):
            journal = directory / role / "chat.json"
            if not journal.exists():
                continue
            turns = json.loads(journal.read_text())["turns"]
            events = [event for t in turns for event in t["episode"]["events"]]
            row[role + "_tokens"] = available_sum(
                token_count(e.get("usage") or {}) for e in events if e["kind"] == "model"
            )
            row[role + "_time_s"] = available_sum(t.get("elapsed_s") for t in turns)
            row[role + "_leaf_calls"] = sum(t["episode"]["usage"]["leaf_calls"] for t in turns)
            row[role + "_macro_invocations"] = sum(e["kind"] == "macro" for e in events)
        native = row.get("native") or {}
        row["correction_turns"] = max(0, native["turns"] - 1) if "turns" in native else None
        trajectory = row.get("trajectory") or {}
        session = (trajectory.get("identity") or {}).get("session")
        records = [
            r
            for r in current
            if r.get("session") == session and r.get("role") == "trajectory_judge"
        ]
        row["trajectory_time_s"] = (
            available_sum(
                r["finished"] - r["started"] if "finished" in r else None for r in records
            )
            if records
            else None
        )
        row["native_success"] = native.get("success")
        row["content_score"] = native.get("content_score")
        row["trajectory_score"] = trajectory.get("score")
        row["trajectory_tokens"] = token_count((row.get("trajectory") or {}).get("usage") or {})
        row["task_time_s"] = (
            row["finished"] - row["started"] if "finished" in row and "started" in row else None
        )
    paused = False
    try:
        owner.shared.check()
    except BudgetExceeded:
        paused = True
    value = dict(
        protocol="full-lift-local-v1",
        rows=rows,
        required_episodes=required_count,
        settled_required_episodes=completed,
        warmup_required=len(owner.scenes) * 4,
        warmup_settled=sum(r["status"] in ("completed", "failed") for r in warmup),
        holdout_cells_required=len(owner.scenes) * 6,
        holdout_cells_settled=sum(r["status"] in ("completed", "failed") for r in holdout),
        coverage_complete=coverage,
        evaluation_healthy=healthy,
        c_quality_satisfied=quality,
        acceptance_complete=full
        and not paused
        and coverage
        and healthy
        and quality
        and costs["model_evidence"] == "host_transport",
        full_scope=full,
        status="paused"
        if paused
        else "finished_with_failures"
        if coverage and not healthy
        else "complete"
        if coverage
        else "incomplete",
        model_evidence=costs["model_evidence"],
        roles=costs["roles"],
        global_roles=costs["global_roles"],
        current_run_http_attempts=costs["current_http_attempts"],
        current_evolution_roles={
            k: v
            for k, v in costs["roles"].items()
            if k not in ("worker", "judge", "trajectory_judge")
        },
        actual_http_attempts=costs["actual_http_attempts"],
        observed_test_scenes=["Sales_Ops_Weekly_Analytics_new"],
        trajectory_score_semantics="native_separate_from_content_success",
        leaf_count_semantics="expanded_primitive_calls_including_F",
        selections=[
            json.loads(p.read_text()) for p in sorted((owner.root / "selections").glob("*.json"))
        ],
    )
    path = owner.root / "full-matrix-report.json"
    if not path.exists() or json.loads(path.read_text()) != value:
        atomic_json(path, value)
    csv_path = owner.root / "full-matrix-report.csv"
    fields = (
        "task_id",
        "arm",
        "phase",
        "status",
        "healthy",
        "native_success",
        "content_score",
        "trajectory_score",
        "error",
        "worker_tokens",
        "judge_tokens",
        "trajectory_tokens",
        "worker_time_s",
        "judge_time_s",
        "trajectory_time_s",
        "task_time_s",
        "worker_leaf_calls",
        "worker_macro_invocations",
        "correction_turns",
    )
    if not csv_path.exists() or csv_path.stat().st_mtime < path.stat().st_mtime:
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: row.get(k) for k in fields} for row in rows)
    return value
