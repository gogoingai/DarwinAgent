"""Physical full-matrix coverage, quality and measured cost reporting."""

import csv
import json

from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest

from .report import available_sum, token_count, turn_totals
from .turn_evidence import recorded_turns


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


def trajectory_receipts_match(trajectory, records, root):
    """Bind every scalar reassessment to its retained output and measured usage."""
    from pathlib import Path

    from .full_trajectory import saturated_empty_completion, scalar_outcome

    if trajectory.get("status") != "completed":
        return False
    attempts = trajectory.get("attempts")
    if attempts is None:  # Original one-send result format remains readable.
        return (
            len(records) == 1
            and records[0].get("status") == "completed"
            and records[0].get("actual_http_attempts", 0) > 0
            and records[0].get("output") == trajectory.get("raw_response")
        )
    try:
        policy = trajectory["identity"].get("retry_policy")
        if policy not in {"known-invalid-scalar-v1", "known-invalid-scalar-v2"}:
            return False
        policy_keys = {"retry_policy", "max_attempts"}
        if policy == "known-invalid-scalar-v2":
            if (
                trajectory["identity"].get("stop_on_saturated_empty") is not True
                or type(trajectory["identity"].get("completion_cap")) is not int
                or trajectory["identity"]["completion_cap"] != 16384
            ):
                return False
            policy_keys |= {"stop_on_saturated_empty", "completion_cap"}
        if (
            not 1 <= len(attempts) <= 3
            or len(records) != len(attempts)
            or trajectory["identity"].get("max_attempts") != 3
            or json.loads((Path(root) / "result.json").read_text()) != trajectory
        ):
            return False
        child_identity = {k: v for k, v in trajectory["identity"].items() if k not in policy_keys}
        total_usage = {}
        for index, (attempt, record) in enumerate(zip(attempts, records)):
            name = f"attempts/{index:03d}/result.json"
            if attempt["path"] != name:
                return False
            child = json.loads((Path(root) / name).read_text())
            if (
                policy == "known-invalid-scalar-v2"
                and index < len(attempts) - 1
                and saturated_empty_completion(child, 16384)
            ):
                return False
            if (
                child["sha256"] != digest({k: v for k, v in child.items() if k != "sha256"})
                or attempt["sha256"] != child["sha256"]
                or child["identity"] != child_identity
                or scalar_outcome(child)[0]
                != ("completed" if index == len(attempts) - 1 else "invalid_response")
                or attempt["status"] != child["status"]
                or attempt["usage"] != child.get("usage", {})
                or record.get("output") != child["raw_response"]
                or record.get("usage") != child.get("usage", {})
                or record.get("status") != "completed"
                or record.get("actual_http_attempts", 0) <= 0
            ):
                return False
            for key, value in child.get("usage", {}).items():
                if type(value) in (int, float):
                    total_usage[key] = total_usage.get(key, 0) + value
        return (
            trajectory["usage"] == total_usage
            and trajectory["raw_response"] == child["raw_response"]
            and trajectory["score"] == scalar_outcome(child)[1]
            and trajectory["sha256"]
            == digest({k: v for k, v in trajectory.items() if k != "sha256"})
        )
    except (ValueError, TypeError, KeyError, OSError):
        return False


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
    from .retry_evidence import read_rate_audit, settled_completions

    try:
        retry_audit = (
            read_rate_audit(owner.shared.root)
            if any(r.get("status") != "completed" for r in current_requests)
            else None
        )
        groups = {}
        for record in current_requests:
            key = tuple(record.get(field) for field in ("run_id", "session", "role"))
            groups.setdefault(key, []).append(record)
        for records in groups.values():
            settled_completions(records, retry_audit)
    except (ValueError, TypeError, KeyError, OSError):
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
            try:
                records = settled_completions(records, retry_audit)
            except (ValueError, TypeError, KeyError):
                return False
            if not events or len(events) != len(records):
                return False
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
        try:
            records = settled_completions(records, retry_audit)
        except (ValueError, TypeError, KeyError):
            return False
        if not trajectory_receipts_match(trajectory, records, directory / "trajectory"):
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
            turns = recorded_turns(directory / role)
            if not turns:
                continue
            events = [event for t in turns for event in t["episode"]["events"]]
            totals = [
                t.get("accounting")
                or turn_totals(directory / role, t.get("evidence_turn_index", index), t)
                for index, t in enumerate(turns)
            ]
            row[role + "_tokens"] = available_sum(t["tokens"] for t in totals)
            row[role + "_known_tokens_subtotal"] = sum(t["known_tokens_subtotal"] for t in totals)
            row[role + "_unknown_token_requests"] = available_sum(
                t["unknown_token_requests"] for t in totals
            )
            row[role + "_time_s"] = available_sum(t.get("elapsed_s") for t in turns)
            row[role + "_leaf_calls"] = available_sum(t["usage"]["leaf_calls"] for t in totals)
            for key in ("unknown_calls", "pending_calls"):
                row[role + "_" + key] = available_sum(t["usage"][key] for t in totals)
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
    if getattr(owner, "task_policy", None) is not None:
        value["protocol"] = "full-lift-local-bounded-v1"
        value["task_execution_policy"] = owner.task_policy.declaration()
    value["confirmation_policy"] = "q5-health-before-q6-v1"
    value["q6_confirmation_ready"] = owner.development_ready()
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
