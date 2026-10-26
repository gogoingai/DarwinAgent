import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, file_tools
from tests.support.agent_runtime import RawRecordedClient


@dataclass(frozen=True)
class VisibleReAct(ReActAgent):
    expose_execution_budget: bool = True


class BudgetVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_reads_remaining_budget_and_terminal_replay_is_identical(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tools = file_tools(root / "files")
            (root / "files/in.txt").write_text("public")
            client = RawRecordedClient(
                [
                    json.dumps({"tool": "read_file", "params": {"path": "in.txt"}}),
                    json.dumps({"final_response": "done"}),
                ]
            )
            profile = ExecutionProfile(
                "visible", tuple(t.name for t in tools), max_model_calls=3, max_leaf_calls=1
            )
            backend = ExecutionBackend(VisibleReAct(), tools, profile, client, root / "runs")
            task = AgentTask("q", "read file")
            result = await backend.run(task, run_id="one")
            self.assertEqual(result.status, "completed")
            budgets = []
            for request in client.requests:
                users = [
                    json.loads(m["content"]) for m in request["messages"] if m["role"] == "user"
                ]
                budgets.append(next(u["view"]["execution_budget"] for u in users if "view" in u))
            self.assertEqual(budgets[0]["model"], {"used": 0, "limit": 3, "remaining": 3})
            self.assertEqual(budgets[0]["tool"], {"used": 0, "limit": 1, "remaining": 1})
            self.assertEqual(budgets[1]["tool"], {"used": 1, "limit": 1, "remaining": 0})
            self.assertEqual(budgets[1]["model"]["used"], 1)
            again = await backend.run(task, run_id="one")
            self.assertEqual(again.to_dict(), result.to_dict())
            self.assertEqual(len(client.requests), 2)

    def test_replay_prefix_excludes_future_receipts_and_unbounded_is_explicit(self):
        from darwinagent.agent.context import AgentContext
        from darwinagent.runtime.steps import StepJournal

        with tempfile.TemporaryDirectory() as d:
            journal = StepJournal(Path(d) / "steps")
            for index, kind in enumerate(("model", "tool", "model")):
                (journal.root / f"{index:06d}.json").write_text(json.dumps({"kind": kind}))
            journal.position = 1
            context = AgentContext(
                AgentTask("q", "test"),
                ExecutionProfile("prefix", (), max_model_calls=None, max_leaf_calls=64),
                {},
                None,
                journal,
            )
            self.assertEqual(
                context.execution_budget()["model"], {"used": 1, "limit": None, "remaining": None}
            )
            self.assertEqual(context.execution_budget()["tool"]["remaining"], 64)
            journal.position = 2
            self.assertEqual(context.execution_budget()["tool"]["remaining"], 63)

    async def test_legacy_react_does_not_change_message_contract(self):
        with tempfile.TemporaryDirectory() as d:
            client = RawRecordedClient([json.dumps({"final_response": "done"})])
            backend = ExecutionBackend(
                ReActAgent(), [], ExecutionProfile("old", ()), client, Path(d)
            )
            await backend.run(AgentTask("q", "finish"), run_id="old")
            self.assertNotIn("execution_budget", json.dumps(client.requests))


if __name__ == "__main__":
    unittest.main()
