"""Optional full-task caps persist across native corrections without global pauses."""

import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.llm.client import BudgetExceeded
from datasets.lift.task_budget import TaskExecutionPolicy, scope_task_worker
from datasets.lift.transport import SharedTransport
from tests.support.full_lift_transport import FakeLLMClient


class TaskBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_send_resource_reservation_is_preserved_without_consuming_http_cap(self):
        from darwinagent.llm.client import ResourceBudgetExceeded

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeLLMClient(root)
            original = client.chat
            rejected = False

            async def chat(**kwargs):
                nonlocal rejected
                if not rejected:
                    rejected = True
                    raise ResourceBudgetExceeded("synthetic no-send resource refusal")
                return await original(**kwargs)

            client.chat = chat
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            policy = TaskExecutionPolicy(3, 2)
            with self.assertRaises(ResourceBudgetExceeded):
                await scope_task_worker(shared, "task", "worker", policy).chat(
                    role="worker", messages=[]
                )
            for _ in range(2):
                await scope_task_worker(shared, "task", "worker", policy).chat(
                    role="worker", messages=[]
                )
            with self.assertRaises(BudgetExceeded):
                await scope_task_worker(shared, "task", "worker", policy).chat(
                    role="worker", messages=[]
                )
            self.assertEqual(client.http_attempts(), 2)
            self.assertEqual(json.loads((shared.root / "worker-calls.json").read_text())["task"], 3)
            records = [json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")]
            self.assertEqual(len(records), 3)
            self.assertEqual(sum(r["actual_http_attempts"] for r in records), 2)
            self.assertEqual(sum(r["status"] == "budget_exhausted" for r in records), 1)
            self.assertFalse((shared.root / "service-rate-pending.json").exists())

    async def test_task_cap_survives_new_scope_and_other_tasks_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeLLMClient(root)
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            policy = TaskExecutionPolicy(3, 2)
            for _ in range(2):
                await scope_task_worker(shared, "task-one", "worker", policy).chat(
                    role="worker", messages=[]
                )
            with self.assertRaises(BudgetExceeded) as caught:
                await scope_task_worker(shared, "task-one", "worker", policy).chat(
                    role="worker", messages=[]
                )
            self.assertFalse(caught.exception.dispatched)
            self.assertFalse((shared.root / "service-rate-pending.json").exists())
            self.assertFalse((shared.root / "pause.json").exists())
            self.assertEqual(client.http_attempts(), 2)
            await scope_task_worker(shared, "task-two", "worker", policy).chat(
                role="worker", messages=[]
            )
            self.assertEqual(client.http_attempts(), 3)
            self.assertEqual(
                json.loads((shared.root / "worker-calls.json").read_text()),
                {"task-one": 2, "task-two": 1},
            )

    def test_policy_rejects_invalid_and_ambiguous_budget_values(self):
        for rounds, limit in [(True, 8), (0, 8), (31, 8), (3, True), (3, 0), (3, None)]:
            with self.subTest(rounds=rounds, limit=limit), self.assertRaises(ValueError):
                TaskExecutionPolicy(rounds, limit)

    def test_declaration_roundtrip_rejects_unknown_fields_and_extra_retries(self):
        policy = TaskExecutionPolicy(3, 8)
        self.assertEqual(TaskExecutionPolicy.from_declaration(policy.declaration()), policy)
        for patch in [
            {"extra": True},
            {"known_rejection_attempts": 3},
            {"known_rejection_attempts": True},
        ]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                TaskExecutionPolicy.from_declaration(dict(policy.declaration(), **patch))
