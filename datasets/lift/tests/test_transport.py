"""Cumulative sessions and unknown quarantine; real HTTP probes live in http_budget_probe."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.llm.client import BudgetExceeded
from datasets.lift.transport import SharedTransport
from tests.support.lift_transport import Recorded


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_limit_survives_new_scope_and_shared_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = Recorded(root / "campaign-a")
            for _ in range(8):
                shared = SharedTransport(client, root / "shared")
                await shared.scoped("one-task", "worker").chat(role="worker", messages=[])
            with self.assertRaises(BudgetExceeded):
                await (
                    SharedTransport(client, root / "shared")
                    .scoped("one-task", "worker")
                    .chat(role="worker", messages=[])
                )
            self.assertEqual(client.calls, 8)

    async def test_unknown_quarantines_a_different_campaign_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = Recorded(root / "campaign-a", unknown=True)
            shared = SharedTransport(first, root / "shared")
            with self.assertRaises(ConnectionError):
                await shared.scoped("task-a", "worker").chat(role="worker", messages=[])
            second = Recorded(root / "campaign-b")
            with self.assertRaisesRegex(BudgetExceeded, "unknown"):
                await (
                    SharedTransport(second, root / "shared")
                    .scoped("task-b", "worker")
                    .chat(role="worker", messages=[])
                )
            self.assertEqual(second.calls, 0)
            self.assertTrue((root / "shared/pause.json").exists())

    async def test_crash_between_submission_and_receipt_blocks_new_campaign(self):
        from darwinagent.runtime.artifacts import atomic_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = Recorded(root / "new-campaign")
            shared = SharedTransport(client, root / "shared")
            atomic_json(root / "shared/requests/lost.json", {"status": "submitted"})
            with self.assertRaisesRegex(BudgetExceeded, "unknown"):
                await shared.scoped("different-task", "worker").chat(role="worker", messages=[])
            self.assertEqual(client.calls, 0)
