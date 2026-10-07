"""Experiment recovery helpers; independent of the controller."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from darwinagent.contracts import plain
from darwinagent.runtime.artifacts import atomic_json, digest

_TRANSIENT_ERRORS = frozenset(
    {
        "InternalServerError",
        "APIStatusError",
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
    }
)
_DETERMINISTIC_ERRORS = frozenset({"SandboxError", "ValueError", "TypeError", "KeyError"})


async def batched_fault_retry(
    pipeline,
    case,
    spec,
    config,
    answers_dir,
    faulted,
    sleep=asyncio.sleep,
    batch_size=25,
    lead_s=150.0,
    gap_s=60.0,
):
    """One bounded retry pass for faulted questions: wait out the transient-burst window,
    then archive original checkpoint bytes and clear the current view in small batches.
    Rerun the case (healthy
    questions checkpoint-reuse at zero cost). The final fault set is recomputed from the
    LAST complete answer set — never a union of per-batch snapshots: batches not yet retried
    still carry their stale fault checkpoints, and a union would preserve those pre-retry
    states as phantom faults (review #4, offline-reproduced)."""
    await sleep(lead_s)
    from darwinagent.runtime.artifacts import digest as _digest

    result = None
    for start in range(0, len(faulted), batch_size):
        for a in faulted[start : start + batch_size]:
            checkpoint = Path(answers_dir) / f"{_digest(a.question_id)}.json"
            if checkpoint.exists():
                import hashlib

                from darwinagent.runtime.continuation import _immutable_copy

                original = checkpoint.read_bytes()
                archive = (
                    Path(answers_dir)
                    / "history"
                    / checkpoint.stem
                    / (hashlib.sha256(original).hexdigest() + ".json")
                )
                _immutable_copy(archive, original)
                # Unlink only after the original bytes are durable; retry writes a new view.
                checkpoint.unlink()
        result = await pipeline.run(case, spec, config)
        if start + batch_size < len(faulted):
            await sleep(gap_s)
    still_faulted = sorted(a.question_id for a in result.answers if a.status == "execution_error")
    return result, still_faulted


def _retryable_answer(answer):
    if any(ev.get("stage") == "tool_error" for ev in plain(answer.trace or ())):
        return False
    kind = str(answer.error).split(":", 1)[0].strip()
    if kind in _DETERMINISTIC_ERRORS:
        return False
    return kind in _TRANSIENT_ERRORS or str(answer.error).startswith(
        ("TransportExhausted:", "EmptyCompletion:")
    )


def _retry_journal(path, identity):
    if path.exists():
        journal = json.loads(path.read_text())
        if journal["identity"] != identity:
            raise ValueError("Retry budget belongs to a different run identity")
        return journal
    return {"identity": identity, "questions": {}}


def _settle_reservations(path, result, consumed):
    if not path.exists():
        return
    journal = _retry_journal(path, result.identity)
    for answer in result.answers:
        previous = journal["questions"].get(answer.question_id)
        if previous is not None and previous["state"] == "reserved":
            previous.update(
                state="done",
                consumed_after=consumed,
                recovered_from_reservation=True,
                final_error_type=str(answer.error).split(":", 1)[0]
                if answer.status == "execution_error"
                else None,
            )
    atomic_json(path, journal)


def _failed_tool_params(results):
    """Only training failures, with the original executed action when recorded."""
    entries = []
    for result in results or ():
        for answer in result.answers:
            if answer.status != "execution_error":
                continue
            for event in plain(answer.trace or ()):
                if event.get("stage") == "tool_error":
                    entries.append((result.case_id, event["asset_id"], event["parameters"]))
            # Older checkpoints lack tool_error; the final action is usable only
            # when its asset matches a deterministic tool failure.
            if not any(e.get("stage") == "tool_error" for e in plain(answer.trace or ())) and str(
                answer.error
            ).startswith(("SandboxError:", "ValueError:")):
                for raw in reversed(answer.raw_outputs or ()):
                    try:
                        action = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(action, dict) and action.get("action") == "call":
                        entries.append(
                            (result.case_id, action.get("asset_id"), action.get("parameters"))
                        )
                        break
    return entries


def _prior_failed_tool_params(root):
    """Replay saved failures from earlier stages, including rejected candidates."""
    entries = []
    seen = set()
    for path in sorted(Path(root).glob("*/generation/*/answers/*.json")):
        answer = json.loads(path.read_text()).get("result", {})
        if answer.get("status") != "execution_error":
            continue
        case_id = path.parent.parent.name
        actions = [
            (event.get("asset_id"), event.get("parameters"))
            for event in answer.get("trace", ())
            if event.get("stage") == "tool_error"
        ]
        if not actions and str(answer.get("error", "")).startswith(
            ("SandboxError:", "ValueError:")
        ):
            for raw in reversed(answer.get("raw_outputs", ())):
                try:
                    action = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(action, dict) and action.get("action") == "call":
                    actions = [(action.get("asset_id"), action.get("parameters"))]
                    break
        for aid, params in actions:
            if aid is None or params is None:
                continue
            key = (case_id, aid, digest(plain(params)))
            if key not in seen:
                seen.add(key)
                entries.append((case_id, aid, params))
    return entries


def _check_snapshot_expectation(snapshot, answer_contract):
    """机器可判定的快照分类（零语义判定）：answered 但答案为空/不可解析、或可解析但
    违反任务 answer_contract → 任何 C 都必须拒（must_reject）；类型合法但被拒 →
    informational——合法的语义拒绝不自动判成代码 bug，只有经具体复现验证
    （旧指纹在合法快照上拒＋候选通过＋非法孪生仍拒）才由 verified 存储提升为
    verified_must_pass。abstained 不受 answer_contract 约束（拒答不是答案）。"""
    from darwinagent.kernel.spec import validate_value

    status = snapshot.get("status")
    if status != "answered":
        return "informational", ""
    answer = snapshot.get("answer")
    structured = snapshot.get("structured_answer")
    value = structured
    if value is None and isinstance(answer, str) and answer:
        try:
            value = json.loads(answer)
        except ValueError:
            value = None
    if not answer and value is None:
        return "must_reject", "answered with empty payload and no evidence"
    if value is None:
        return "must_reject", "answered payload is not parseable JSON"
    if answer_contract:
        try:
            validate_value(value, answer_contract, "answer")
        except ValueError as exc:
            return "must_reject", f"violates task answer_contract: {exc}"
    return "informational", ""


def _prior_failed_check_snapshots(root, answer_contract=None):
    """Scan all historical answer checkpoints (every stage, including candidates whose
    bundle was later rejected by the decision) for C-rejection events that persisted a
    check snapshot → replay rows. Mirrors _prior_failed_tool_params; the C counterpart
    of the F-parameter replay library (do not rebuild F params here)."""
    root = Path(root)
    verified = {}
    store = root / "optimization" / "check-replay-verified.json"
    if store.exists():
        try:
            for row in json.loads(store.read_text()).get("rows", ()):
                verified[row.get("snapshot_digest")] = row
        except (OSError, ValueError):
            pass
    rows = []
    seen = set()
    for path in sorted(root.glob("*/generation/*/answers/*.json")):
        case_id = path.parent.parent.name
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        answer = stored.get("result", {})
        question_id = answer.get("question_id")
        for event in plain(answer.get("trace") or ()):
            snapshot = event.get("check_snapshot")
            if event.get("stage") != "candidate" or not isinstance(snapshot, dict):
                continue
            failed = [c for c in event.get("checks", ()) if not c.get("ok")]
            if not failed:
                continue
            snapshot_digest = digest(snapshot)
            if snapshot_digest in seen:
                continue
            seen.add(snapshot_digest)
            promoted = verified.get(snapshot_digest)
            if promoted:
                expectation, reason = "verified_must_pass", str(promoted.get("provenance", {}))
            else:
                expectation, reason = _check_snapshot_expectation(snapshot, answer_contract)
            rows.append(
                {
                    "case_id": case_id,
                    "question_id": question_id,
                    "check_ids": sorted({c.get("check_id") for c in failed if c.get("check_id")}),
                    "issues": sorted({i for c in failed for i in c.get("issues", ())}),
                    "snapshot": snapshot,
                    "snapshot_digest": snapshot_digest,
                    "expectation": expectation,
                    "reason": reason,
                    "source": str(path),
                }
            )
    return rows


def promote_verified_check_replay(root, entries):
    """Persist reproduced-check verifications: each entry binds one snapshot digest to a
    real reproduction (old fingerprint rejected the legal snapshot, the fixed candidate
    passes it, and the malformed twin is still rejected). Verified rows become required
    admission replay for every later candidate of the same check assets."""
    root = Path(root)
    store = root / "optimization" / "check-replay-verified.json"
    current = {"rows": []}
    if store.exists():
        try:
            current = json.loads(store.read_text())
        except (OSError, ValueError):
            current = {"rows": []}
    known = {row.get("snapshot_digest") for row in current.get("rows", ())}
    for entry in entries:
        if entry.get("snapshot_digest") in known:
            continue
        current["rows"].append(
            {
                "snapshot_digest": entry["snapshot_digest"],
                "expectation": "must_pass",
                "check_ids": sorted(entry.get("check_ids", ())),
                "verified_by": entry.get("verified_by", "reproduction"),
                "provenance": entry.get("provenance", {}),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        known.add(entry["snapshot_digest"])
    store.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(store, current)
    return current
