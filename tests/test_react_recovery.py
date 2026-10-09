"""Known runtime feedback stays in the same bounded ReAct loop; unknowns stop it."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, file_tools
from tests.support.agent_runtime import RecordedClient


def delivery_check(stage, candidate):
    return stage != "final_delivery" or candidate["final_response"] == "valid"


class ReActRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_read_failure_then_delivery_rejection_recover_with_fixed_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RecordedClient(
                [
                    {"tool": "read_file", "params": {"path": "missing.txt"}},
                    {"final_response": "invalid"},
                    {"final_response": "valid"},
                ]
            )
            tools = file_tools(root / "files")
            backend = ExecutionBackend(
                ReActAgent(max_turns=4),
                tools,
                ExecutionProfile(
                    "recovery", tuple(t.name for t in tools), host_checks=(delivery_check,)
                ),
                client,
                root / "receipts",
            )
            task = AgentTask("q", "Deliver valid text")
            result = await backend.run(task, run_id="first")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.final_response, "valid")
            self.assertEqual(client.calls, 3)
            failure = next(e for e in result.events if e["kind"] == "tool_failure")
            self.assertEqual(failure["failure"]["tool_name"], "read_file")
            replay = await backend.run(task, run_id="first")
            self.assertEqual(replay.to_dict(), result.to_dict())
            self.assertEqual(client.calls, 3)

    async def test_unknown_write_cannot_be_corrected_or_redispatched(self):
        from dataclasses import replace

        async def lost_write(params):
            raise ConnectionError("write may have completed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RecordedClient(
                [
                    {"tool": "write_file", "params": {"path": "x", "content": "x"}},
                    {"final_response": "valid"},
                ]
            )
            read, write = file_tools(root / "files")
            backend = ExecutionBackend(
                ReActAgent(),
                [read, replace(write, handler=lost_write)],
                ExecutionProfile("unknown", ("read_file", "write_file")),
                client,
                root / "receipts",
            )
            task = AgentTask("q", "write")
            self.assertEqual((await backend.run(task, run_id="first")).status, "unknown")
            self.assertEqual((await backend.run(task, run_id="first")).status, "unknown")
            self.assertEqual(client.calls, 1)

    async def test_rejected_delivery_obeys_existing_model_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = RecordedClient([{"final_response": "invalid"}])
            backend = ExecutionBackend(
                ReActAgent(),
                [],
                ExecutionProfile("cap", (), max_model_calls=1, host_checks=(delivery_check,)),
                client,
                Path(temporary),
            )
            result = await backend.run(AgentTask("q", "deliver"), run_id="first")
            self.assertEqual(result.status, "budget_exhausted")
            self.assertEqual(client.calls, 1)
