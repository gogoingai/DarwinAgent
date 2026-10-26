import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import APIStatusError

from datasets.lift import rate_control
from datasets.lift.full_report import role_costs
from tests.support.full_lift_transport import FullTransportFixture


class RateControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = FullTransportFixture(Path(temp.name))
        self.clock = 1000.0
        self.waits = []

    async def wait(self, seconds):
        self.assertLessEqual(seconds, 30)
        self.waits.append(seconds)
        self.clock += seconds

    def client_failures(self, count):
        client = self.fixture.client
        original = client.chat

        async def chat(**kwargs):
            response = await original(**kwargs)
            if len(client.calls) <= count:
                raise APIStatusError(
                    "offline",
                    response=httpx.Response(
                        429, request=httpx.Request("POST", "https://example.invalid")
                    ),
                    body={"code": "rate_limit_exceeded"},
                )
            return response

        client.chat = chat

    async def test_known429_waits_and_each_actual_attempt_retains_its_receipt(self):
        shared = self.fixture.full()
        self.assertIsInstance(
            shared.scoped("new", "worker"), rate_control.RateLimitedSessionTransport
        )
        self.client_failures(2)
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
        ):
            result = await shared.scoped("new", "worker").chat(role="worker", messages=[])
        self.assertEqual(result.content, "synthetic")
        self.assertEqual(len(self.fixture.client.calls), 3)
        self.assertEqual(self.fixture.client.http_attempts(), 310)
        self.assertEqual(sum(self.waits), 180)
        records = [
            json.loads(p.read_text())
            for p in (self.fixture.shared_root / "requests").glob("*.json")
            if p.name != "original.json"
        ]
        self.assertEqual(sorted(r["status"] for r in records), ["completed", "failed", "failed"])
        self.assertEqual(role_costs(records)["worker"]["actual_http_attempts"], 3)
        self.assertEqual(
            (self.fixture.shared_root / "requests/original.json").read_bytes(),
            self.fixture.receipt_bytes,
        )
        self.assertEqual(
            (self.fixture.shared_root / "policy.json").read_bytes(), self.fixture.original_bytes
        )

    async def test_three_rejections_stop_and_reopen_honors_global_cooldown(self):
        shared = self.fixture.full()
        self.assertIsInstance(
            shared.scoped("new", "worker"), rate_control.RateLimitedSessionTransport
        )
        self.client_failures(3)
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
        ):
            with self.assertRaises(APIStatusError):
                await shared.scoped("new", "worker").chat(role="worker", messages=[])
            self.assertEqual(len(self.fixture.client.calls), 3)
            reopened = self.fixture.full()
            await reopened.scoped("other-role", "judge").chat(role="judge", messages=[])
        self.assertEqual(len(self.fixture.client.calls), 4)
        self.assertEqual(sum(self.waits), 420)
        state = json.loads((self.fixture.shared_root / "service-rate-control.json").read_text())
        self.assertEqual(state["consecutive_429"], 0)

    async def test_unknown_transport_is_never_retried(self):
        self.fixture.client.unknown = True
        shared = self.fixture.full()
        self.assertIsInstance(
            shared.scoped("new", "worker"), rate_control.RateLimitedSessionTransport
        )
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
        ):
            with self.assertRaises(ConnectionError):
                await shared.scoped("new", "worker").chat(role="worker", messages=[])
        self.assertEqual(len(self.fixture.client.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertTrue(
            (self.fixture.shared_root / "active-pause.json").exists()
            or (self.fixture.shared_root / "pause.json").exists()
        )

    async def test_non429_status_never_retries(self):
        shared = self.fixture.full()
        original = self.fixture.client.chat

        async def reject(**kwargs):
            await original(**kwargs)
            raise APIStatusError(
                "offline",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://example.invalid")
                ),
                body={},
            )

        self.fixture.client.chat = reject
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
        ):
            with self.assertRaises(APIStatusError):
                await shared.scoped("new", "worker").chat(role="worker", messages=[])
        self.assertEqual(len(self.fixture.client.calls), 1)
        self.assertEqual(self.waits, [])

    async def test_corrupt_cooldown_fails_before_any_dispatch(self):
        shared = self.fixture.full()
        (self.fixture.shared_root / "service-rate-control.json").write_text(
            '{"protocol":"changed"}'
        )
        with self.assertRaises(ValueError):
            await shared.scoped("new", "worker").chat(role="worker", messages=[])
        self.assertEqual(self.fixture.client.calls, [])

    async def test_crash_after_event_before_cache_recovers_wait(self):
        shared = self.fixture.full()
        self.client_failures(1)
        session = shared.scoped("new", "worker")
        original = session._save
        writes = []

        def crash(state):
            writes.append(state)
            if state["event_count"] == 1:
                raise OSError("offline simulated crash")
            original(state)

        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
            patch.object(session, "_save", crash),
        ):
            with self.assertRaises(OSError):
                await session.chat(role="worker", messages=[])
        self.assertEqual(len(self.fixture.client.calls), 1)
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
        ):
            await self.fixture.full().scoped("reopened", "judge").chat(role="judge", messages=[])
        self.assertEqual(sum(self.waits), 60)
        self.assertEqual(len(self.fixture.client.calls), 2)

    async def test_crash_after_receipt_before_event_recovers_wait(self):
        shared = self.fixture.full()
        self.client_failures(1)
        session = shared.scoped("new", "worker")
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
            patch.object(session, "_commit", side_effect=OSError("offline crash")),
        ):
            with self.assertRaises(OSError):
                await session.chat(role="worker", messages=[])
        self.assertEqual(len(self.fixture.client.calls), 1)
        with (
            patch.object(rate_control, "wall_time", lambda: self.clock),
            patch.object(rate_control, "wait_cooldown", self.wait),
        ):
            await self.fixture.full().scoped("reopened", "judge").chat(role="judge", messages=[])
        self.assertEqual(sum(self.waits), 60)
        self.assertEqual(len(self.fixture.client.calls), 2)
