"""Complete local worker transcripts and durable native LIFT trajectory judging.

Raw request snapshots are retained as evidence. The chronological projection
removes repeated conversation prefixes, permitting only ReAct's documented view
replacement. No model call, service discovery or dotenv read occurs on import.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from copy import deepcopy
from pathlib import Path

from darwinagent.agent.react import invalid_reply_excerpt
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.leases import execution_lease
from darwinagent.runtime.steps import StepJournal, UnknownRequest


class IncompleteTrajectory(ValueError):
    """Evidence cannot establish a complete settled chronological transcript."""


def _sha(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _read(path, sources):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    sources[str(path)] = _sha(path)
    return value


def _without_view(message):
    value = deepcopy(message)
    if value.get("role") == "user" and isinstance(value.get("content"), str):
        try:
            content = json.loads(value["content"])
        except ValueError:
            return value
        if isinstance(content, dict):
            content.pop("view", None)
            value["content"] = content
    return value


def _numbered(directory):
    paths = sorted(Path(directory).glob("[0-9]*.json"), key=lambda p: int(p.stem))
    if [int(p.stem) for p in paths] != list(range(len(paths))):
        raise IncompleteTrajectory("Noncontiguous durable journal")
    return paths


def extract_trajectory(session_root, *, episode_roots=None, include_snapshots=True):
    """Read terminal worker sessions, including native corrections and F leaves.

    ``episode_roots`` when supplied must equal all recorded turn roots in order;
    it cannot select a subset. Every source file is pinned for judging/replay.
    ``include_snapshots=False`` retains source references instead of embedding
    every full request and episode again; chronological messages stay complete.
    """
    root = Path(session_root).resolve()
    sources, messages, requests = {}, [], []
    inventories = {}
    episode_records = []
    identity = _read(root / "session-identity.json", sources)
    if identity["role"] != "worker":
        raise IncompleteTrajectory("Only worker evidence belongs in a trajectory")
    chat = _read(root / "chat.json", sources)
    turns = chat["turns"]
    paths = _numbered(root / "turns")
    inventories[str(root / "turns")] = sorted(p.name for p in (root / "turns").glob("*.json"))
    inventories[str(root / "episodes")] = sorted(p.name for p in (root / "episodes").glob("turn-*"))
    episodes = [root / "episodes" / f"turn-{i}" for i in range(len(turns))]
    if episode_roots is not None and [Path(p).resolve() for p in episode_roots] != episodes:
        raise IncompleteTrajectory("Episode roots differ from complete native turn history")
    if not turns or len(paths) != len(turns) or len(chat["history"]) != 2 * len(turns):
        raise IncompleteTrajectory("Missing native turn receipts or history")
    if {p.resolve() for p in (root / "episodes").glob("turn-*")} != set(episodes):
        raise IncompleteTrajectory("Unaccounted episode roots")
    leaves, model_calls = 0, 0
    for i, episode in enumerate(episodes):
        native_user, native_final = chat["history"][2 * i : 2 * i + 2]
        if native_user["role"] != "user" or native_final["role"] != "assistant":
            raise IncompleteTrajectory("Invalid native message order")
        turn = _read(paths[i], sources)
        result = _read(episode / "result.json", sources)
        ep = result["result"]
        if result["digest"] != digest(ep) or ep != turns[i]["episode"] or ep != turn["episode"]:
            raise IncompleteTrajectory("Episode/turn receipt mismatch")
        if (
            turn["message_hash"] != digest(native_user["content"])
            or turns[i]["message_hash"] != turn["message_hash"]
        ):
            raise IncompleteTrajectory("Native correction prompt receipt mismatch")
        if ep["status"] == "unknown" or ep["final_response"] != native_final["content"]:
            raise IncompleteTrajectory("Unsettled native final response")
        episode_identity = _read(episode / "identity.json", sources)
        if (
            episode_identity["kernel_id"] != identity["kernel_id"]
            or episode_identity["task"]["id"] != identity["task_id"]
        ):
            raise IncompleteTrajectory("Episode identity mismatch")
        if (
            episode_identity["task"]["parameters"].get("native_messages")
            != chat["history"][: 2 * i + 1]
        ):
            raise IncompleteTrajectory("Episode native history mismatch")
        events = []
        for directory in (episode / "steps", episode / "events"):
            inventories[str(directory)] = sorted(p.name for p in directory.glob("[0-9]*.json"))
        for event_path in _numbered(episode / "events"):
            row = _read(event_path, sources)
            event = row["event"]
            if digest(event) != row["digest"] or event["index"] != len(events):
                raise IncompleteTrajectory("Execution event integrity mismatch")
            events.append(event)
        if events != ep["events"]:
            raise IncompleteTrajectory("Episode events differ from durable records")
        model_rows = [e for e in events if e["kind"] == "model"]
        projections = {}
        for event in events:
            if event["kind"] != "context_projection":
                continue
            parent = event.get("source_model_index")
            if (
                type(parent) is not int
                or parent < 0
                or parent != event["index"] - 1
                or events[parent]["kind"] != "model"
                or parent in projections
                or event.get("projection") != "invalid_reply_excerpt_v1"
                or event.get("error_type") not in {"JSONDecodeError", "ActionProtocolError"}
                or type(event.get("cap")) is not int
                or event["cap"] <= 0
            ):
                raise IncompleteTrajectory("Invalid context projection evidence")
            projections[parent] = event
        messages.append(native_user)
        previous, model_events, tool_steps = [], [], {}
        expected_order = []
        recorded_steps = []
        steps = _numbered(episode / "steps")
        journal = StepJournal(episode / "steps", create_root=False)
        retry_root = journal.root / "model-retries"
        if retry_root.exists():
            if retry_root.is_symlink():
                raise IncompleteTrajectory("Retry evidence cannot be a symlink")
            inventories[str(retry_root)] = sorted(p.name for p in retry_root.glob("*.json"))
            for retry_evidence in sorted(retry_root.glob("*.json")):
                if retry_evidence.is_symlink():
                    raise IncompleteTrajectory("Retry evidence cannot be a symlink")
                _read(retry_evidence, sources)
        for step in steps:
            row = _read(step, sources)
            active_path, active_row = journal.read_active(step)
            if active_path != step:
                row = _read(active_path, sources)
                if row != active_row:
                    raise IncompleteTrajectory("Authorized recovery changed during extraction")
            if include_snapshots:
                recorded_steps.append({"name": step.name, "record": row})
            if row["state"] != "responded" or digest(row["response"]) != row["response_digest"]:
                raise IncompleteTrajectory("Missing or unsettled model/tool receipt")
            if row.get("previous_requests"):
                raise IncompleteTrajectory(
                    "Historical replacement requests require complete original receipts"
                )
            payload = row["input"]
            semantic = (
                {k: v for k, v in payload.items() if k not in {"temperature", "max_tokens"}}
                if row["kind"] == "model"
                else payload
            )
            if digest({"kind": row["kind"], "input": semantic}) != row["fingerprint"]:
                raise IncompleteTrajectory("Step input fingerprint mismatch")
            if row["kind"] == "model":
                if payload["role"] != "worker":
                    raise IncompleteTrajectory("Judge/proposer evidence in worker journal")
                current = payload["messages"]
                control_message = None
                if current and current[-1].get("role") == "user":
                    try:
                        marker = json.loads(current[-1]["content"])
                    except (TypeError, json.JSONDecodeError):
                        marker = None
                    if (
                        isinstance(marker, dict)
                        and set(marker) == {"execution_control"}
                        and isinstance(marker["execution_control"], dict)
                        and isinstance(marker["execution_control"].get("policy"), dict)
                        and marker["execution_control"]["policy"].get("kind") == "task-control-v1"
                    ):
                        control_message = current[-1]
                        current = current[:-1]
                if len(current) < len(previous) or [
                    _without_view(m) for m in current[: len(previous)]
                ] != [_without_view(m) for m in previous]:
                    raise IncompleteTrajectory("Model transcript changed beyond view replacement")
                messages.extend(deepcopy(current[len(previous) :]))
                if control_message is not None:
                    messages.append(deepcopy(control_message))
                response = row["response"]
                assistant = {
                    "role": "assistant",
                    "content": response["content"],
                    "usage": response.get("usage", {}),
                }
                messages.append(assistant)
                previous = deepcopy(current) + [
                    {"role": "assistant", "content": response["content"]}
                ]
                model_event = model_rows[len(model_events)]
                projection = projections.get(model_event["index"])
                if projection is not None:
                    projected = invalid_reply_excerpt(response["content"], projection["cap"])
                    if (
                        projection.get("original_digest") != digest(response["content"])
                        or projection.get("message_index") != len(current)
                        or projection.get("projected_content") != projected
                    ):
                        raise IncompleteTrajectory("Context projection differs from raw receipt")
                    previous[-1]["content"] = projected
                model_calls += 1
                if include_snapshots:
                    requests.append(
                        {"episode": i, "step": step.name, "input": payload, "response": response}
                    )
                model_events.append(digest(response["content"]))
                expected_order.append(("model", digest(response["content"])))
            elif row["kind"] == "tool":
                leaves += 1
                tool_steps[step.name] = row
                expected_order.extend(
                    [
                        ("tool_call", step.name),
                        ("tool_failure" if "failure" in row["response"] else "tool", step.name),
                    ]
                )
                call_id = f"turn-{i}/{step.stem}"
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": call_id,
                                "name": payload["name"],
                                "arguments": payload["params"],
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "toolName": payload["name"],
                        "toolCallId": call_id,
                        "content": json.dumps(row["response"], ensure_ascii=False),
                    }
                )
            else:
                raise IncompleteTrajectory("Unknown durable step kind")
        actual_order = [
            (e["kind"], e["response_digest"] if e["kind"] == "model" else e["receipt"])
            for e in events
            if e["kind"] in {"model", "tool_call", "tool", "tool_failure"}
        ]
        if actual_order != expected_order:
            raise IncompleteTrajectory("Execution chronology differs from step journal")
        if model_events != [e["response_digest"] for e in events if e["kind"] == "model"]:
            raise IncompleteTrajectory("Missing model steps/events")
        for name, row in tool_steps.items():
            calls = [e for e in events if e["kind"] == "tool_call" and e.get("receipt") == name]
            ends = [
                e
                for e in events
                if e["kind"] in {"tool", "tool_failure"} and e.get("receipt") == name
            ]
            if (
                len(calls) != 1
                or len(ends) != 1
                or calls[0]["name"] != row["input"]["name"]
                or calls[0]["params"] != row["input"]["params"]
            ):
                raise IncompleteTrajectory("Missing expanded primitive call/result events")
            expected = (
                {"failure": ends[0]["failure"]}
                if ends[0]["kind"] == "tool_failure"
                else {"output": ends[0]["output"]}
            )
            if row["response"] != expected:
                raise IncompleteTrajectory("Primitive result differs from receipt")
        if len([e for e in events if e["kind"] == "tool_call"]) != len(tool_steps):
            raise IncompleteTrajectory("Unreceipted primitive execution event")
        if (
            len(tool_steps) != ep["usage"]["leaf_calls"]
            or len(model_events) != ep["usage"]["model_calls"]
        ):
            raise IncompleteTrajectory("Episode usage does not reconcile")
        if include_snapshots:
            episode_records.append(
                {
                    "identity": episode_identity,
                    "turn": turn,
                    "result": result,
                    "events": events,
                    "steps": recorded_steps,
                }
            )
        else:
            episode_records.append(
                {
                    "identity": {
                        "kernel_id": episode_identity["kernel_id"],
                        "task": {k: episode_identity["task"][k] for k in ("id", "instruction")},
                    },
                    "turn_source": str(paths[i]),
                    "result_source": str(episode / "result.json"),
                    "status": ep["status"],
                    "usage": ep["usage"],
                }
            )
        messages.append(native_final)
    value = {
        "schema": "full-lift-trajectory-v1",
        "identity": identity,
        "all_messages": messages,
        "episodes": episode_records,
        "leaf_calls": leaves,
        "source_hashes": sources,
        "source_inventory": inventories,
    }
    if include_snapshots:
        value["raw_requests"] = requests
    else:
        value.update(
            schema="full-lift-trajectory-v2", session_root=str(root), model_calls=model_calls
        )
    return dict(value, sha256=digest(value))


def _verify(envelope):
    value = {k: v for k, v in envelope.items() if k != "sha256"}
    if envelope.get("sha256") != digest(value):
        raise ValueError("Trajectory envelope changed")
    for directory, names in envelope.get("source_inventory", {}).items():
        pattern = (
            "turn-*"
            if Path(directory).name == "episodes"
            else ("*.json" if Path(directory).name == "turns" else "[0-9]*.json")
        )
        if sorted(p.name for p in Path(directory).glob(pattern)) != names:
            raise ValueError("Pinned trajectory inventory changed")
    for path, checksum in envelope["source_hashes"].items():
        if _sha(path) != checksum:
            raise ValueError("Pinned trajectory source changed")
    if envelope.get("schema") == "full-lift-trajectory-v2":
        expected = extract_trajectory(envelope["session_root"], include_snapshots=False)
        if envelope != expected:
            raise ValueError("Referenced trajectory differs from complete source journals")


def _native_judge():
    # Upstream config calls load_dotenv on import; disable discovery first.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    from src.postprocess import judge

    return judge


def build_native_judge_input(
    transcript,
    *,
    task_query,
    trajectory_reqs,
    source_hashes,
    reference_projection=False,
    chunk_projection=False,
    nested_reference_projection=False,
    value_projection=False,
    readable_value_projection=False,
):
    """Use upstream's loaded native prompt builder; pin its exact source bytes."""
    import pandas as pd

    judge = _native_judge()

    _verify(transcript)
    if task_query != transcript["episodes"][0]["identity"]["task"]["instruction"]:
        raise ValueError("Judge task query differs from actual worker task")
    native_source = str(Path(judge.__file__).resolve())
    if source_hashes.get(native_source) != _sha(native_source):
        raise ValueError("Native judge source must be explicitly pinned")
    projected, projection = transcript["all_messages"], None
    if readable_value_projection and not value_projection:
        raise ValueError("Readable display requires the complete value reference protocol")
    if value_projection and (
        reference_projection or chunk_projection or nested_reference_projection
    ):
        raise ValueError("Value references use their own complete input protocol")
    if nested_reference_projection and not (reference_projection and chunk_projection):
        raise ValueError("Nested references require the chunk and reference protocols")
    if nested_reference_projection:
        from .judge_projection import project_nested_tool_observations

        projected, projection = project_nested_tool_observations(projected)
    elif reference_projection:
        from .judge_projection import project_tool_observations

        projected, projection = project_tool_observations(projected)
    chunks = None
    if chunk_projection:
        if not reference_projection:
            raise ValueError("Chunk projection requires reference projection")
        from .trace_chunks import encode

        projected = deepcopy(projected)
        indices = [i for i, message in enumerate(projected) if message.get("content") is not None]
        chunks = encode([judge.format_content_blocks(projected[i]["content"]) for i in indices])
        for i, rendered in zip(indices, chunks["rendered"], strict=True):
            projected[i]["content"] = rendered
    values = None
    if value_projection:
        from .trace_values import decode, encode, render

        if readable_value_projection:
            from .readable_trace import render_readable as render

        projected = deepcopy(projected)
        indices = [i for i, message in enumerate(projected) if message.get("content") is not None]
        texts = [judge.format_content_blocks(projected[i]["content"]) for i in indices]
        values = encode(texts)
        if decode(values) != texts:
            raise ValueError("Value projection lost original trajectory content")
        for i, rendered in zip(indices, render(values), strict=True):
            projected[i]["content"] = rendered
    messages = judge.build_judge_messages(
        pd.Series(
            {
                "task_query": task_query,
                "trajectory_reqs": trajectory_reqs,
                "all_messages": projected,
            }
        )
    )
    value = {
        "schema": "full-lift-judge-input-v1",
        "task_id": transcript["identity"]["task_id"],
        "kernel_id": transcript["identity"]["kernel_id"],
        "transcript_sha256": transcript["sha256"],
        "transcript": transcript,
        "messages": messages,
        "source_hashes": dict(transcript["source_hashes"], **source_hashes),
    }
    if reference_projection:
        from .judge_projection import REFERENCE_GUIDE, nested_reference_guide

        if projection["entries"]:
            guide = (
                nested_reference_guide(projection, transcript["all_messages"])
                if nested_reference_projection
                else REFERENCE_GUIDE
            )
            messages[1]["content"] = guide + messages[1]["content"]
        value.update(
            schema="full-lift-judge-input-v2",
            trajectory_projection=projection,
            trajectory_requirements=trajectory_reqs,
        )
    if chunk_projection:
        from .trace_chunks import GUIDE

        messages[1]["content"] = GUIDE + messages[1]["content"]
        value.update(schema="full-lift-judge-input-v3", text_chunks=chunks)
    if nested_reference_projection:
        value["schema"] = "full-lift-judge-input-v4"
    if value_projection:
        from .trace_values import GUIDE

        if readable_value_projection:
            from .readable_trace import GUIDE

        messages[1]["content"] = GUIDE + messages[1]["content"]
        value.update(
            schema="full-lift-judge-input-v6"
            if readable_value_projection
            else "full-lift-judge-input-v5",
            value_refs=values,
            trajectory_requirements=trajectory_reqs,
        )
    return dict(value, sha256=digest(value))


def verify_native_judge_projection(judge_input):
    """Bind the new representation to the native builder and immutable transcript."""
    if judge_input.get("schema") not in {
        "full-lift-judge-input-v2",
        "full-lift-judge-input-v3",
        "full-lift-judge-input-v4",
        "full-lift-judge-input-v5",
        "full-lift-judge-input-v6",
    }:
        if any(k in judge_input for k in ("trajectory_projection", "text_chunks", "value_refs")):
            raise ValueError("Judge projection requires its declared input protocol")
        return
    transcript = judge_input["transcript"]
    expected = build_native_judge_input(
        transcript,
        task_query=transcript["episodes"][0]["identity"]["task"]["instruction"],
        trajectory_reqs=judge_input["trajectory_requirements"],
        source_hashes=judge_input["source_hashes"],
        reference_projection=judge_input["schema"]
        not in {"full-lift-judge-input-v5", "full-lift-judge-input-v6"},
        chunk_projection=judge_input["schema"]
        in {"full-lift-judge-input-v3", "full-lift-judge-input-v4"},
        nested_reference_projection=judge_input["schema"] == "full-lift-judge-input-v4",
        value_projection=judge_input["schema"]
        in {"full-lift-judge-input-v5", "full-lift-judge-input-v6"},
        readable_value_projection=judge_input["schema"] == "full-lift-judge-input-v6",
    )
    if judge_input != expected:
        raise ValueError("Judge projection or messages differ from verified native input")


async def score_trajectory(shared, session, root, judge_input):
    """One paid send via shared ledger; terminal replay never sends or retries.

    An interrupted submission remains unknown and raises on resume. A local
    budget rejection remains a durable failure; a fresh attempt requires a new
    explicit result directory. Transport policy and model bind every receipt.
    """
    from openai import APIStatusError

    extract_score_from_response = _native_judge().extract_score_from_response

    if hasattr(shared, "cfg"):
        shared.cfg.model_for("trajectory_judge")
    _verify(judge_input)
    _verify(judge_input["transcript"])
    verify_native_judge_projection(judge_input)
    root = Path(root)
    identity = {
        "session": session,
        "input_sha256": judge_input["sha256"],
        "task_id": judge_input["task_id"],
        "kernel_id": judge_input["kernel_id"],
        "model": shared.identity(),
    }
    with execution_lease(root / "judge.lock"):
        path = root / "result.json"
        if path.exists():
            result = json.loads(path.read_text())
            if result["identity"] != identity or result["sha256"] != digest(
                {k: v for k, v in result.items() if k != "sha256"}
            ):
                raise ValueError("Trajectory judge result identity/integrity changed")
            if result["status"] in {"submitted", "unknown"}:
                raise UnknownRequest("Trajectory judge submission has no settled receipt")
            return result
        atomic_json(root / "input.json", judge_input)
        result = {"identity": identity, "status": "submitted", "score": None, "raw_response": None}

        def save():
            result["sha256"] = digest({k: v for k, v in result.items() if k != "sha256"})
            atomic_json(path, result)

        save()
        try:
            response = await shared.scoped(session, "trajectory_judge").chat(
                messages=judge_input["messages"],
                role="trajectory_judge",
                namespace="full_lift_trajectory",
                json_mode=True,
                use_cache=False,
            )
            result.update(
                raw_response=response.content,
                usage=getattr(response, "usage", {}),
                response_id=getattr(response, "response_id", None),
                provider_model=getattr(response, "provider_model", None),
                requested_model=getattr(response, "requested_model", None),
                status="invalid_response",
            )
            save()
            score = extract_score_from_response(response.content)
            if not math.isfinite(score):
                raise ValueError("Nonfinite native score")
            result.update(status="completed", score=score)
        except (asyncio.CancelledError, Exception) as exc:
            completed = getattr(exc, "completed_result", None)
            if isinstance(exc, asyncio.CancelledError) and completed is not None:
                result.update(raw_response=completed.content, usage=getattr(completed, "usage", {}))
                try:
                    result.update(
                        status="completed", score=extract_score_from_response(completed.content)
                    )
                except (ValueError, TypeError):
                    result.update(status="invalid_response", error_type="ValueError")
                save()
                raise
            if result["raw_response"] is not None:
                result["status"] = "invalid_response"
            elif isinstance(exc, APIStatusError):
                result["status"] = "failed"
            elif isinstance(exc, BudgetExceeded) and not exc.dispatched:
                result["status"] = "budget_exhausted"
            else:
                result["status"] = "unknown"
            result["error_type"] = type(exc).__name__
            save()
            if result["status"] == "unknown":
                raise
            return result
        save()
        return result


def scalar_outcome(row):
    """Interpret saved raw output before retrying a possibly interrupted parse.

    Legacy single-send receipts are immutable. Their intermediate invalid_response
    state may precede parsing, so this derived interpretation belongs in the new
    aggregate only, never back in the original receipt.
    """
    if row["status"] != "invalid_response":
        return row["status"], row.get("score")
    try:
        score = _native_judge().extract_score_from_response(row["raw_response"])
        if math.isfinite(score):
            return "completed", score
    except (ValueError, TypeError):
        pass
    return "invalid_response", None


def saturated_empty_completion(row, completion_cap):
    usage = row.get("usage") or {}
    tokens = usage.get("completion_tokens")
    raw = row.get("raw_response")
    return (
        row.get("status") == "invalid_response"
        and type(raw) is str
        and not raw.strip()
        and usage.get("empty_content") is True
        and type(tokens) is int
        and tokens >= completion_cap
    )


async def score_trajectory_with_retries(
    shared, session, root, judge_input, *, stop_on_saturated_empty=False
):
    """Reassess only settled invalid outputs, at most three scalar attempts.

    Each scalar attempt retains its result in its own directory. Confirmed HTTP
    rejections may use the transport's separately recorded three-send policy;
    hence this wrapper permits at most nine physical sends, all ledger charged.
    Cancellation/unknown submissions propagate; resumption reads settled children
    before sending. The aggregate includes every attempt's usage, including empty
    outputs. This policy never replays the worker, tools, or native content judge.
    """
    _verify(judge_input)
    _verify(judge_input["transcript"])
    verify_native_judge_projection(judge_input)
    root = Path(root)
    child_identity = {
        "session": session,
        "input_sha256": judge_input["sha256"],
        "task_id": judge_input["task_id"],
        "kernel_id": judge_input["kernel_id"],
        "model": shared.identity(),
    }
    identity = dict(child_identity, retry_policy="known-invalid-scalar-v1", max_attempts=3)
    completion_cap = getattr(shared, "generation_max_tokens", None)
    if stop_on_saturated_empty:
        if type(completion_cap) is not int or completion_cap <= 0:
            raise ValueError("Saturated-empty policy requires a fixed generation token cap")
        identity.update(
            retry_policy="known-invalid-scalar-v2",
            stop_on_saturated_empty=True,
            completion_cap=completion_cap,
        )

    def saturated_empty(row):
        return stop_on_saturated_empty and saturated_empty_completion(row, completion_cap)

    def aggregate(rows):
        usage = {}
        for row in rows:
            for key, value in row.get("usage", {}).items():
                if type(value) in (int, float):
                    usage[key] = usage.get(key, 0) + value
        result = dict(rows[-1])
        status, score = scalar_outcome(rows[-1])
        result.update(
            identity=identity,
            status=status,
            score=score,
            usage=usage,
            attempts=[
                {
                    "path": f"attempts/{i:03d}/result.json",
                    "sha256": row["sha256"],
                    "status": row["status"],
                    "usage": row.get("usage", {}),
                }
                for i, row in enumerate(rows)
            ],
        )
        result.pop("sha256", None)
        if saturated_empty(rows[-1]):
            result["retry_stop_reason"] = "empty_completion_at_cap"
        return dict(result, sha256=digest(result))

    with execution_lease(root / "retry.lock"):
        input_path = root / "input.json"
        if input_path.exists():
            if json.loads(input_path.read_text()) != judge_input:
                raise ValueError("Trajectory retry input changed")
        else:
            atomic_json(input_path, judge_input)
        attempts_root = root / "attempts"
        existing = sorted(attempts_root.iterdir()) if attempts_root.exists() else []
        if [p.name for p in existing] != [f"{i:03d}" for i in range(len(existing))] or len(
            existing
        ) > 3:
            raise ValueError("Trajectory retry attempt inventory changed")
        final_path = root / "result.json"
        rows = []
        if final_path.exists():
            for directory in existing:
                row = json.loads((directory / "result.json").read_text())
                if (
                    row.get("identity") != child_identity
                    or row.get("sha256") != digest({k: v for k, v in row.items() if k != "sha256"})
                    or json.loads((directory / "input.json").read_text()) != judge_input
                ):
                    raise ValueError("Trajectory retry attempt identity/integrity changed")
                rows.append(row)
            if (
                not rows
                or any(
                    scalar_outcome(r)[0] != "invalid_response" or saturated_empty(r)
                    for r in rows[:-1]
                )
                or scalar_outcome(rows[-1])[0] in {"submitted", "unknown"}
                or (
                    scalar_outcome(rows[-1])[0] == "invalid_response"
                    and len(rows) != 3
                    and not saturated_empty(rows[-1])
                )
            ):
                raise ValueError("Trajectory retry terminal inventory invalid")
            expected = aggregate(rows)
            if json.loads(final_path.read_text()) != expected:
                raise ValueError("Trajectory retry aggregate changed")
            return expected
        for index in range(3):
            directory = attempts_root / f"{index:03d}"
            saved_input = directory / "input.json"
            if saved_input.exists() and json.loads(saved_input.read_text()) != judge_input:
                raise ValueError("Trajectory retry attempt input changed")
            row = await score_trajectory(shared, session, directory, judge_input)
            rows.append(row)
            if scalar_outcome(row)[0] != "invalid_response" or saturated_empty(row) or index == 2:
                if len(existing) > index + 1:
                    raise ValueError("Unexpected attempts after settled terminal judge")
                result = aggregate(rows)
                atomic_json(final_path, result)
                return result
