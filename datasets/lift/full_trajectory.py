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

from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.leases import execution_lease
from darwinagent.runtime.steps import UnknownRequest


class IncompleteTrajectory(ValueError):
    """Evidence cannot establish a complete settled chronological transcript."""


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def extract_trajectory(session_root, *, episode_roots=None):
    """Read terminal worker sessions, including native corrections and F leaves.

    ``episode_roots`` when supplied must equal all recorded turn roots in order;
    it cannot select a subset. Every source file is pinned for judging/replay.
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
    leaves = 0
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
        messages.append(native_user)
        previous, model_events, tool_steps = [], [], {}
        expected_order = []
        recorded_steps = []
        steps = _numbered(episode / "steps")
        for step in steps:
            row = _read(step, sources)
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
                if len(current) < len(previous) or [
                    _without_view(m) for m in current[: len(previous)]
                ] != [_without_view(m) for m in previous]:
                    raise IncompleteTrajectory("Model transcript changed beyond view replacement")
                messages.extend(deepcopy(current[len(previous) :]))
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
        episode_records.append(
            {
                "identity": episode_identity,
                "turn": turn,
                "result": result,
                "events": events,
                "steps": recorded_steps,
            }
        )
        messages.append(native_final)
    value = {
        "schema": "full-lift-trajectory-v1",
        "identity": identity,
        "all_messages": messages,
        "raw_requests": requests,
        "episodes": episode_records,
        "leaf_calls": leaves,
        "source_hashes": sources,
        "source_inventory": inventories,
    }
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


def _native_judge():
    # Upstream config calls load_dotenv on import; disable discovery first.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    from src.postprocess import judge

    return judge


def build_native_judge_input(transcript, *, task_query, trajectory_reqs, source_hashes):
    """Use upstream's loaded native prompt builder; pin its exact source bytes."""
    import pandas as pd

    judge = _native_judge()

    _verify(transcript)
    if task_query != transcript["episodes"][0]["identity"]["task"]["instruction"]:
        raise ValueError("Judge task query differs from actual worker task")
    native_source = str(Path(judge.__file__).resolve())
    if source_hashes.get(native_source) != _sha(native_source):
        raise ValueError("Native judge source must be explicitly pinned")
    messages = judge.build_judge_messages(
        pd.Series(
            {
                "task_query": task_query,
                "trajectory_reqs": trajectory_reqs,
                "all_messages": transcript["all_messages"],
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
    return dict(value, sha256=digest(value))


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
