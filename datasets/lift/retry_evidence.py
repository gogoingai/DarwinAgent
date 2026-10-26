"""Read-only correspondence between logical calls and settled physical attempts."""

import json
import math
from pathlib import Path

from darwinagent.runtime.artifacts import digest

from .rate_control import MAX_ATTEMPTS, PROTOCOL


def _read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate audit key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("Nonfinite audit constant")

    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=nonfinite)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_rate_audit(root):
    """Never repair caches, remove pending intents, or rewrite any receipt."""
    root = Path(root)
    if (root / "service-rate-pending.json").exists():
        raise ValueError("Service request intent is unsettled")
    state = {"protocol": PROTOCOL, "event_count": 0, "consecutive_429": 0, "cooldown_until": 0.0}
    audited = {}
    for index, path in enumerate(sorted((root / "service-rate-events").glob("*.json")), 1):
        event = _read(path)
        checksum = event.pop("sha256", None)
        if (
            checksum != digest(event)
            or path.name != f"{index:08d}.json"
            or event.get("previous_state_sha256") != digest(state)
        ):
            raise ValueError("Rate event chain changed")
        name = event.get("receipt")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or "\\" in name
            or not name.endswith(".json")
        ):
            raise ValueError("Invalid rate receipt reference")
        receipt = _read(root / "requests" / name)
        identifier = receipt.get("id")
        if (
            type(identifier) is not str
            or name != identifier + ".json"
            or identifier in audited
            or event.get("receipt_sha256") != digest(receipt)
        ):
            raise ValueError("Rate receipt binding changed")
        if (
            type(receipt.get("actual_http_attempts")) is not int
            or receipt["actual_http_attempts"] != 1
        ):
            raise ValueError("Rate event does not represent one actual attempt")
        following = event.get("state", {})
        count, deadline = following.get("consecutive_429"), following.get("cooldown_until")
        if (
            set(following) != set(state)
            or following.get("protocol") != PROTOCOL
            or type(following.get("event_count")) is not int
            or following["event_count"] != index
            or type(count) is not int
            or count < 0
            or not _finite(deadline)
            or deadline < 0
        ):
            raise ValueError("Invalid rate event state")
        if (
            receipt.get("status") == "failed"
            and type(receipt.get("http_status")) is int
            and receipt["http_status"] == 429
        ):
            if count != state["consecutive_429"] + 1 or deadline <= 0:
                raise ValueError("Invalid rejection transition")
        elif (
            receipt.get("status") == "failed"
            and type(receipt.get("http_status")) is int
            and receipt["http_status"] in {500, 502, 503, 504, 529}
        ):
            if count != state["consecutive_429"] or deadline != state["cooldown_until"]:
                raise ValueError("Server rejection changed quota cooldown")
        elif receipt.get("status") == "completed":
            if count != 0 or deadline != 0:
                raise ValueError("Invalid successful reset")
        else:
            raise ValueError("Rate event is not a known settled outcome")
        audited[identifier] = {"index": index, "receipt_sha256": digest(receipt)}
        state = following
    cached = _read(root / "service-rate-control.json")
    checksum = cached.pop("sha256", None)
    if checksum != digest(cached) or cached != state:
        raise ValueError("Rate cache and immutable events disagree")
    return audited


def settled_completions(records, audit=None):
    """Reject unproven attempts rather than dropping them from measured costs."""
    records = list(records)
    if any(
        not _finite(r.get("started"))
        or not _finite(r.get("finished"))
        or r["finished"] < r["started"]
        for r in records
    ):
        raise ValueError("Unsettled or invalid attempt chronology")
    records.sort(key=lambda r: r["started"])
    completed, pending, identifiers = [], [], set()
    previous_finished = None
    fields = (
        "run_id",
        "session",
        "role",
        "namespace",
        "request_fingerprint",
        "max_tokens",
        "model",
        "provider_request_policy",
    )
    for row in records:
        identifier = row.get("id")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or type(row.get("actual_http_attempts")) is not int
            or row["actual_http_attempts"] != 1
        ):
            raise ValueError("Ambiguous physical attempt identity")
        if previous_finished is not None and row["started"] <= previous_finished:
            raise ValueError("Physical attempts overlap")
        identifiers.add(identifier)
        previous_finished = row["finished"]
        status = row.get("status")
        if (
            status == "failed"
            and type(row.get("http_status")) is int
            and row["http_status"] in {429, 500, 502, 503, 504, 529}
            and row.get("output") is None
            and row.get("usage") is None
        ):
            pending.append(row)
            if len(pending) >= MAX_ATTEMPTS:
                raise ValueError("Known rejection exhausted the logical request")
            continue
        if status != "completed":
            raise ValueError("Unknown or nonretryable attempt cannot supply host evidence")
        if pending:
            fingerprint = row.get("request_fingerprint")
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
                or any(
                    type(row.get(key)) is not str or not row[key]
                    for key in ("run_id", "session", "role", "model")
                )
                or type(row.get("max_tokens")) is not int
                or row["max_tokens"] <= 0
                or row.get("provider_request_policy") != "minimax-adaptive-v1"
            ):
                raise ValueError("Retry lacks the fixed physical input identity")
            batch = [*pending, row]
            positions = []
            for attempt in batch:
                if any(attempt.get(key) != row.get(key) for key in fields):
                    raise ValueError("Retry input or execution identity changed")
                proof = (audit or {}).get(attempt["id"])
                if not proof or proof.get("receipt_sha256") != digest(attempt):
                    raise ValueError("Retry lacks an immutable rate-event binding")
                positions.append(proof["index"])
            if positions != list(range(positions[0], positions[0] + len(batch))):
                raise ValueError("Retry event sequence is not contiguous")
            pending.clear()
        completed.append(row)
    if pending:
        raise ValueError("Retry chain has no successful terminal response")
    return completed
