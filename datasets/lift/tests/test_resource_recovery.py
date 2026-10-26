"""Disk exhaustion salvages a received response without another provider call."""

import errno
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from darwinagent.llm.client import BudgetExceeded, LLMClient
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.resources import ResourceGuard, ResourceLimit
from datasets.lift.rate_control import RateLimitedSessionTransport
from datasets.lift.transport import SharedTransport
from tests.support.full_lift_transport import FakeLLMClient


class ResourceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_resource_factory_prepares_before_attachment(self):
        from datasets.lift.full_resources import DEFAULT_LIMITS, prepare_resources

        with tempfile.TemporaryDirectory() as tmp:
            client = SimpleNamespace()
            with patch("datasets.lift.full_resources.ResourceGuard") as factory:
                guard = prepare_resources(
                    {"run_root": tmp, "resource_limits": dict(DEFAULT_LIMITS)}, client
                )
                factory.assert_called_once_with(
                    Path(tmp).resolve() / "resource-reserve", **DEFAULT_LIMITS
                )
                guard.prepare.assert_called_once()
                self.assertIs(client.resource_guard, guard)

    def test_resource_policy_cannot_silently_weaken_or_add_fields(self):
        from datasets.lift.full_resources import DEFAULT_LIMITS, resource_limits

        for change in [{"min_free_bytes": 1}, {"max_rss_bytes": None}, {"extra": True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                resource_limits({"resource_limits": dict(DEFAULT_LIMITS, **change)})

    async def test_real_client_request_hook_rejects_without_dispatch_or_unknown(self):
        from darwinagent.llm.client import ResourceBudgetExceeded
        from tests.support.judge_retry import offline_transport

        def factory(cfg, http_client):
            with patch("darwinagent.llm.client.httpx.AsyncClient", new=http_client):
                return LLMClient(cfg)

        with tempfile.TemporaryDirectory() as tmp:
            client, _, requests = offline_transport(Path(tmp), [], client_factory=factory)
            client.resource_guard = SimpleNamespace(
                check=unittest.mock.Mock(
                    side_effect=ResourceLimit("synthetic request hook pressure")
                )
            )
            try:
                with self.assertRaises(ResourceBudgetExceeded) as caught:
                    await client.chat(role="worker", messages=[], durable=True)
                self.assertFalse(caught.exception.dispatched)
                self.assertEqual(client.http_attempts(), 0)
                self.assertEqual(requests, [])
            finally:
                await client.aclose()

    async def test_resource_pressure_is_global_and_does_not_create_permanent_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard = ResourceGuard(root, reserve_bytes=4096, min_free_bytes=0, max_rss_bytes=None)
            guard.prepare()
            client = FakeLLMClient(root)
            client.resource_guard = guard
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            try:
                with patch.object(guard, "check", side_effect=ResourceLimit("synthetic pressure")):
                    with self.assertRaises(BudgetExceeded):
                        shared.check()
                self.assertFalse((shared.root / "pause.json").exists())
                self.assertFalse((shared.root / "active-pause.json").exists())
                shared.check()
                self.assertEqual(client.http_attempts(), 0)
            finally:
                guard.close()

    async def test_resource_race_after_rate_intent_is_archived_without_unknown_or_send(self):
        from darwinagent.llm.client import ResourceBudgetExceeded

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeLLMClient(root)
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            transport = RateLimitedSessionTransport(shared, "pilot", "worker", max_attempts=1)
            with patch(
                "datasets.lift.transport.SessionTransport._chat",
                side_effect=ResourceBudgetExceeded("pressure before receipt"),
            ):
                with self.assertRaises(ResourceBudgetExceeded):
                    await transport.chat(role="worker", messages=[])
            self.assertEqual(client.http_attempts(), 0)
            self.assertFalse((shared.root / "service-rate-pending.json").exists())
            self.assertEqual(
                len(list((shared.root / "service-preflight-rejections").glob("*.json"))), 1
            )
            shared.check()

    async def test_run_cleanup_releases_own_reserve_after_failure(self):
        from datasets.lift.local import run_and_close

        with tempfile.TemporaryDirectory() as tmp:
            guard = ResourceGuard(tmp, reserve_bytes=4096, min_free_bytes=0, max_rss_bytes=None)
            guard.prepare()
            client = SimpleNamespace(resource_guard=guard, aclose=AsyncMock())
            runner = SimpleNamespace(
                shared=SimpleNamespace(client=client),
                run=AsyncMock(side_effect=ValueError("synthetic stopped run")),
            )
            with self.assertRaisesRegex(ValueError, "synthetic stopped run"):
                await run_and_close(runner)
            client.aclose.assert_awaited_once()
            self.assertFalse(guard.path.exists())

    async def test_rate_control_resource_rejection_creates_no_pending_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeLLMClient(root)
            client.resource_guard = ResourceGuard(root, reserve_bytes=4096, min_free_bytes=0)
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            transport = RateLimitedSessionTransport(shared, "pilot", "worker", max_attempts=1)
            with self.assertRaises(BudgetExceeded):
                await transport.chat(role="worker", messages=[])
            self.assertEqual(client.http_attempts(), 0)
            self.assertFalse((shared.root / "service-rate-pending.json").exists())

    async def test_physical_dispatch_guard_blocks_before_budget_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = LLMClient.__new__(LLMClient)
            client.cfg = SimpleNamespace(deadline_monotonic=None, max_http_requests=None)
            client._http_budget_path = root / "http.json"
            client.resource_guard = ResourceGuard(root, reserve_bytes=4096, min_free_bytes=0)
            with self.assertRaises(BudgetExceeded) as caught:
                await client._reserve_http_attempt(None)
            self.assertFalse(caught.exception.dispatched)
            self.assertFalse(client._http_budget_path.exists())

    async def test_insufficient_space_blocks_before_reserve_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ResourceGuard(tmp, reserve_bytes=4096, min_free_bytes=8192)
            with patch("darwinagent.runtime.resources.shutil.disk_usage") as usage:
                usage.return_value.free = 4096
                with self.assertRaises(ResourceLimit):
                    guard.prepare()
            self.assertFalse(guard.path.exists())

    async def test_unrelated_write_error_does_not_consume_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ResourceGuard(tmp, reserve_bytes=4096, min_free_bytes=0, max_rss_bytes=None)
            guard.prepare()
            try:
                with self.assertRaises(PermissionError):
                    guard.persist_json(
                        Path(tmp) / "receipt.json",
                        {},
                        writer=unittest.mock.Mock(
                            side_effect=PermissionError(errno.EACCES, "denied")
                        ),
                    )
                self.assertFalse(guard.exhausted)
                self.assertTrue(guard.path.exists())
            finally:
                guard.close()

    async def test_completed_receipt_is_salvaged_and_next_dispatch_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard = ResourceGuard(root, reserve_bytes=4096, min_free_bytes=0, max_rss_bytes=None)
            guard.prepare()
            client = FakeLLMClient(root)
            client.resource_guard = guard
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            failed = False

            def write(path, value):
                nonlocal failed
                if value.get("status") == "completed" and not failed:
                    failed = True
                    raise OSError(errno.ENOSPC, "synthetic disk full")
                atomic_json(path, value)

            with patch("datasets.lift.transport.atomic_json", side_effect=write):
                result = await shared.scoped("pilot", "worker").chat(role="worker", messages=[])
            self.assertEqual(result.content, "synthetic")
            self.assertTrue(failed)
            self.assertTrue(guard.exhausted)
            receipts = list((root / "shared/requests").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            saved = json.loads(receipts[0].read_text())
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["output"], "synthetic")
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("pilot", "worker").chat(role="worker", messages=[])
            self.assertEqual(client.http_attempts(), 1)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(len(list((root / "shared/requests").glob("*.json"))), 1)
