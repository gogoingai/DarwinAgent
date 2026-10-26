"""Bounded retries of confirmed HTTP rejections; unknown requests never retry."""

import asyncio
import json
import math
import time
import uuid

from openai import APIStatusError

from darwinagent.llm.client import ResourceBudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.diagnostics import exception_evidence
from darwinagent.runtime.leases import execution_lease

from .transport import SessionTransport

PROTOCOL = "full-lift-known429-cooldown-v1"
MAX_ATTEMPTS = 3


def wall_time():
    return time.time()


async def wait_cooldown(seconds):
    await asyncio.sleep(seconds)


class RateLimitedSessionTransport(SessionTransport):
    """Each retry gets its own original transport receipt and budget charge."""

    def __init__(self, shared, session, role, *, max_attempts=MAX_ATTEMPTS):
        super().__init__(shared, session, role)
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError("Known-rejection attempt limit must be between one and three")
        self.max_attempts = max_attempts

    def _state(self):
        state = {
            "protocol": PROTOCOL,
            "event_count": 0,
            "consecutive_429": 0,
            "cooldown_until": 0.0,
        }
        history = [digest(state)]
        consumed = set()
        for index, path in enumerate(
            sorted((self.shared.root / "service-rate-events").glob("*.json")), 1
        ):
            event = json.loads(path.read_text())
            checksum = event.pop("sha256", None)
            if (
                checksum != digest(event)
                or path.name != f"{index:08d}.json"
                or event.get("previous_state_sha256") != digest(state)
            ):
                raise ValueError("Service cooldown event chain changed")
            receipt_name = event.get("receipt")
            if not isinstance(receipt_name, str) or "/" in receipt_name or "\\" in receipt_name:
                raise ValueError("Invalid service receipt name")
            receipt = json.loads((self.shared.root / "requests" / receipt_name).read_text())
            if event.get("receipt_sha256") != digest(receipt):
                raise ValueError("Service cooldown receipt changed")
            consumed.add(receipt_name)
            following = event.get("state", {})
            count, deadline = following.get("consecutive_429"), following.get("cooldown_until")
            if (
                following.get("protocol") != PROTOCOL
                or following.get("event_count") != index
                or type(count) is not int
                or count < 0
                or type(deadline) not in (int, float)
                or not math.isfinite(deadline)
                or deadline < 0
            ):
                raise ValueError("Invalid service cooldown audit")
            if (
                receipt.get("status") == "failed"
                and receipt.get("http_status") == 429
                and receipt.get("actual_http_attempts") == 1
            ):
                if count != state["consecutive_429"] + 1:
                    raise ValueError("Service rejection count changed")
            elif (
                receipt.get("status") == "failed"
                and receipt.get("http_status") in {500, 502, 503, 504, 529}
                and receipt.get("actual_http_attempts") == 1
            ):
                if count != state["consecutive_429"] or deadline != state["cooldown_until"]:
                    raise ValueError("Server rejection changed quota cooldown")
            elif receipt.get("status") == "completed":
                if count != 0 or deadline != 0:
                    raise ValueError("Service success did not reset cooldown")
            else:
                raise ValueError("Service event lacks settled request evidence")
            state = following
            history.append(digest(state))
        target = self.shared.root / "service-rate-control.json"
        if target.exists():
            cached = json.loads(target.read_text())
            checksum = cached.pop("sha256", None)
            if checksum != digest(cached) or digest(cached) not in history[-2:]:
                raise ValueError("Service cooldown cache changed")
        elif len(history) > 2:
            raise ValueError("Missing established cooldown cache")
        # Events are authoritative: a crash after appending one event is recoverable.
        if not target.exists() or cached != state:
            self._save(state)
        pending_path = self.shared.root / "service-rate-pending.json"
        if pending_path.exists():
            pending = json.loads(pending_path.read_text())
            checksum = pending.pop("sha256", None)
            if checksum != digest(pending) or pending.get("protocol") != PROTOCOL:
                raise ValueError("Service request intent changed")
            before = pending.get("before")
            if not isinstance(before, list) or any(not isinstance(v, str) for v in before):
                raise ValueError("Invalid service intent inventory")
            fresh = {p.name for p in (self.shared.root / "requests").glob("*.json")} - set(before)
            if len(fresh) != 1:
                raise ValueError("Unsettled service request intent; no automatic dispatch")
            name = fresh.pop()
            path = self.shared.root / "requests" / name
            row = json.loads(path.read_text())
            if row.get("run_id") != pending.get("run_id") or row.get("session") != pending.get(
                "session"
            ):
                raise ValueError("Service intent belongs to another request")
            if row.get("status") not in {"completed", "failed", "budget_exhausted"}:
                raise ValueError("Unknown service intent must remain paused")
            if name not in consumed:
                if row.get("status") == "completed":
                    following = {"protocol": PROTOCOL, "consecutive_429": 0, "cooldown_until": 0.0}
                elif row.get("http_status") == 429:
                    if row.get("actual_http_attempts") != 1:
                        raise ValueError("Service rejection lacks single actual attempt")
                    count = state["consecutive_429"] + 1
                    seconds = min(60 * 2 ** min(count - 1, 3), 300)
                    following = {
                        "protocol": PROTOCOL,
                        "consecutive_429": count,
                        "cooldown_until": wall_time() + seconds,
                    }
                elif (
                    row.get("status") == "failed"
                    and row.get("http_status") in {500, 502, 503, 504, 529}
                    and row.get("actual_http_attempts") == 1
                ):
                    following = {k: v for k, v in state.items() if k != "event_count"}
                else:
                    following = None
                if following is not None:
                    self._commit(state, following, path)
                    state = dict(following, event_count=state["event_count"] + 1)
            pending_path.unlink()
        return state

    def _save(self, state):
        atomic_json(
            self.shared.root / "service-rate-control.json", dict(state, sha256=digest(state))
        )

    def _commit(self, previous, state, receipt_path):
        state = dict(state, event_count=previous["event_count"] + 1)
        event = {
            "previous_state_sha256": digest(previous),
            "receipt": receipt_path.name,
            "receipt_sha256": digest(json.loads(receipt_path.read_text())),
            "state": state,
        }
        target = self.shared.root / "service-rate-events" / f"{state['event_count']:08d}.json"
        if target.exists():
            raise ValueError("Service cooldown event already exists")
        atomic_json(target, dict(event, sha256=digest(event)))
        self._save(state)

    async def _wait(self):
        while True:
            self.shared.check()
            with execution_lease(self.shared.root / "service-rate-control.lock"):
                delay = self._state()["cooldown_until"] - wall_time()
            if delay <= 0:
                return
            await wait_cooldown(min(delay, 30))

    def _receipt(self, before):
        fresh = set((self.shared.root / "requests").glob("*.json")) - before
        if len(fresh) != 1:
            raise ValueError("Rate control requires exactly one original receipt")
        path = fresh.pop()
        row = json.loads(path.read_text())
        if row.get("session") != self.session or row.get("run_id") != self.shared.run_id:
            raise ValueError("Rate control receipt belongs to another execution")
        return path, row

    async def _chat(self, kwargs):
        self._check_resources()
        for attempt in range(self.max_attempts):
            await self._wait()
            self._check_resources()
            before = set((self.shared.root / "requests").glob("*.json"))
            before_http = self.shared.client.http_attempts()
            with execution_lease(self.shared.root / "service-rate-control.lock"):
                pending = {
                    "protocol": PROTOCOL,
                    "run_id": self.shared.run_id,
                    "session": self.session,
                    "before": sorted(p.name for p in before),
                }
                atomic_json(
                    self.shared.root / "service-rate-pending.json",
                    dict(pending, sha256=digest(pending)),
                )
            try:
                result = await super()._chat(kwargs)
            except ResourceBudgetExceeded as exc:
                if exc.dispatched:
                    raise
                with execution_lease(self.shared.root / "service-rate-control.lock"):
                    fresh = set((self.shared.root / "requests").glob("*.json")) - before
                    if not fresh and self.shared.client.http_attempts() == before_http:
                        path = self.shared.root / "service-rate-pending.json"
                        original = dict(pending, sha256=digest(pending))
                        if json.loads(path.read_text()) != original:
                            raise ValueError("Resource rejection intent changed") from exc
                        atomic_json(
                            self.shared.root
                            / "service-preflight-rejections"
                            / (uuid.uuid4().hex + ".json"),
                            {
                                "pending": original,
                                "http_attempts": before_http,
                                "diagnostics": exception_evidence(exc),
                                "finished": wall_time(),
                            },
                        )
                        path.unlink()
                    else:
                        self._state()  # A zero-send budget receipt remains preserved.
                raise
            except APIStatusError as exc:
                retryable = exc.status_code in {429, 500, 502, 503, 504, 529}
                if not retryable:
                    with execution_lease(self.shared.root / "service-rate-control.lock"):
                        self._state()
                    raise
                self.shared.check()
                path, row = self._receipt(before)
                if (
                    row.get("status") != "failed"
                    or row.get("http_status") != exc.status_code
                    or row.get("actual_http_attempts") != 1
                ):
                    raise ValueError(
                        "HTTP receipt does not prove a known single rejection"
                    ) from exc
                with execution_lease(self.shared.root / "service-rate-control.lock"):
                    self._state()
                if attempt == self.max_attempts - 1:
                    raise
                if exc.status_code != 429:
                    await wait_cooldown(5 * (attempt + 1))
            else:
                path, row = self._receipt(before)
                if row.get("status") != "completed":
                    raise ValueError("Rate control success lacks completed receipt")
                with execution_lease(self.shared.root / "service-rate-control.lock"):
                    self._state()
                return result
