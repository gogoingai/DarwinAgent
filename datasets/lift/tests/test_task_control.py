"""Real task counters, replayed inputs, and bounded retrieval without provider calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent
from datasets.lift.full_tools import FullLocalTools
from datasets.lift.task_budget import TaskExecutionPolicy, scope_task_worker
from datasets.lift.transport import SharedTransport
from tests.support.full_lift_transport import FakeLLMClient


class TaskControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_turn_remaining_is_in_recorded_request_and_replay_keeps_old_snapshot(self):
        from datasets.lift.task_control import TaskControl, TaskControlPolicy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeLLMClient(root)
            original = client.chat

            async def answer(**kwargs):
                value = await original(**kwargs)
                value.content = '{"final_response":"done"}'
                return value

            client.chat = answer
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            policy = TaskExecutionPolicy(3, 24, control=TaskControlPolicy())

            def backend(owner):
                control = TaskControl(
                    owner, "worker-one", policy.worker_request_limit, policy.control, root / "audit"
                )
                session = scope_task_worker(owner, "worker-one", "worker", policy, control=control)
                return ExecutionBackend(
                    ReActAgent(), (), ExecutionProfile("bounded", ()), session, root / "episodes"
                )

            task = AgentTask("q1", "finish")
            await backend(shared).run(task, run_id="turn-0")
            await backend(shared).run(task, run_id="turn-1")
            snapshots = [
                json.loads(r["messages"][-1]["content"])["execution_control"] for r in client.calls
            ]
            self.assertEqual([s["task_model"]["remaining"] for s in snapshots], [24, 23])
            self.assertEqual([s["task_model"]["used"] for s in snapshots], [0, 1])
            await shared.scoped("other-judge", "judge").chat(role="judge", messages=[])
            reopened = SharedTransport(client, root / "shared", worker_limit=None)
            terminal = root / "episodes/turn-0/result.json"
            terminal.rename(terminal.with_name("retained-terminal.json"))
            replay = await backend(reopened).run(task, run_id="turn-0")
            self.assertEqual(replay.status, "completed")
            self.assertEqual(client.http_attempts(), 3)
            steps = sorted((root / "episodes/turn-0/steps").glob("*.json"))
            model = next(
                json.loads(p.read_text())
                for p in steps
                if json.loads(p.read_text())["kind"] == "model"
            )
            self.assertEqual(
                json.loads(model["input"]["messages"][-1]["content"])["execution_control"][
                    "task_model"
                ]["remaining"],
                24,
            )

    def test_controlled_policy_roundtrip_and_validation(self):
        from datasets.lift.task_control import TaskControlPolicy

        policy = TaskExecutionPolicy(3, 24, control=TaskControlPolicy())
        self.assertEqual(TaskExecutionPolicy.from_declaration(policy.declaration()), policy)
        self.assertEqual(TaskExecutionPolicy(3, 24).declaration()["kind"], "bounded-task-v1")
        for value in [True, 0, -1]:
            with self.assertRaises(ValueError):
                TaskControlPolicy(retrieval_limit=value)
        with self.assertRaises(ValueError):
            TaskExecutionPolicy(3, 4, control=TaskControlPolicy())

    async def test_retrieval_limit_persists_and_write_remains_available(self):
        from datasets.lift.task_control import RetrievalStopped, TaskControl, TaskControlPolicy
        from tests.support.full_lift_web import page

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = SharedTransport(FakeLLMClient(root), root / "shared", worker_limit=None)
            policy = TaskControlPolicy(retrieval_limit=2)
            tools = FullLocalTools(root / "workspace")
            tools.audit_path = root / "audit"
            tools.retrieval_control = TaskControl(shared, "worker", 24, policy, tools.audit_path)
            with patch(
                "datasets.lift.full_tools.fetch_public", return_value=page("<p>public</p>")
            ) as fetch:
                await tools.read_web({"url": "https://example.org/a"})
                await tools.read_web({"url": "https://example.org/b"})
                tools.retrieval_control = TaskControl(
                    shared, "worker", 24, policy, tools.audit_path
                )
                with self.assertRaisesRegex(RetrievalStopped, "retrieval_limit_reached"):
                    await tools.read_web({"url": "https://example.org/c"})
                self.assertEqual(fetch.call_count, 2)
            await tools.write_text(
                {"path": "result/a.txt", "content": "partial evidence with limitations"}
            )
            self.assertTrue((Path(tools.workspace) / "result/a.txt").exists())
            records = [json.loads(p.read_text()) for p in (root / "audit").glob("*.json")]
            self.assertEqual(sum(r["name"] == "read_web" for r in records), 2)

    async def test_delivery_reserve_closes_web_before_network_and_failed_search_does_not_disable_reads(
        self,
    ):
        from datasets.lift.full_tools import WebFailure
        from datasets.lift.task_control import RetrievalStopped, TaskControl, TaskControlPolicy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = SharedTransport(FakeLLMClient(root), root / "shared", worker_limit=None)
            tools = FullLocalTools(root / "workspace")
            tools.audit_path = root / "audit"
            control = TaskControl(
                shared, "worker", 24, TaskControlPolicy(max_failures_per_tool=1), tools.audit_path
            )
            tools.retrieval_control = control
            with patch(
                "datasets.lift.full_tools.fetch_public", side_effect=WebFailure("provider_blocked")
            ) as fetch:
                with self.assertRaises(WebFailure):
                    await tools.search_web({"query": "public reference"})
                with self.assertRaisesRegex(RetrievalStopped, "retrieval_tool_failure_limit"):
                    await tools.search_web({"query": "different query"})
                self.assertNotIn("read_web", control.snapshot(used=1)["blocked_tools"])
                self.assertEqual(control.snapshot(used=1)["phase"], "research")
                self.assertTrue(control.snapshot(used=1)["instruction"].startswith("Gather"))
                shared._actual_worker_attempts = {"worker": 20}
                with self.assertRaisesRegex(RetrievalStopped, "delivery_reserve_reached"):
                    await tools.read_web({"url": "https://example.org/"})
                self.assertEqual(fetch.call_count, 1)
            state = control.snapshot(used=20)
            self.assertEqual(state["phase"], "delivery")
            self.assertEqual(state["task_model"]["remaining"], 4)
