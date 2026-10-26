import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, ToolSpec
from tests.support.agent_runtime import RawRecordedClient
from tests.support.protocol_recovery import BoundedReAct


class ProtocolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_invalid_replies_stop_before_another_request(self):
        client = RawRecordedClient(["{", "{", "{", '{"final_response":"unexpected"}'])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = ExecutionBackend(
                BoundedReAct(max_turns=6), (), ExecutionProfile("fixed", ()), client, root
            )
            result = await backend.run(AgentTask("bad", "Return a result"), run_id="bad")
            self.assertEqual(result.status, "failed")
            self.assertEqual(len(client.requests), 2)
            self.assertEqual(result.usage["model_calls"], 2)
            steps = sorted((root / "bad/steps").glob("*.json"))
            self.assertEqual(len(steps), 2)
            self.assertTrue(all(json.loads(p.read_text())["state"] == "responded" for p in steps))
            self.assertTrue(
                all(json.loads(p.read_text())["response"]["content"] == "{" for p in steps)
            )
            before = [p.read_bytes() for p in steps]
            replay = await backend.run(AgentTask("bad", "Return a result"), run_id="bad")
            self.assertEqual(replay.status, "failed")
            self.assertEqual(len(client.requests), 2)
            self.assertEqual([p.read_bytes() for p in steps], before)

    async def test_ordinary_react_keeps_legacy_protocol_default(self):
        client = RawRecordedClient(["{", "{", '{"final_response":"done"}'])
        with tempfile.TemporaryDirectory() as temp:
            result = await ExecutionBackend(
                ReActAgent(max_turns=4), (), ExecutionProfile("fixed", ()), client, Path(temp)
            ).run(AgentTask("legacy", "Return a result"), run_id="legacy")
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(client.requests), 3)

    async def test_valid_action_resets_consecutive_failure_counter(self):
        replies = ["{", '{"tool":"noop","params":{}}', "{", '{"final_response":"done"}']
        client = RawRecordedClient(replies)
        with tempfile.TemporaryDirectory() as temp:
            tool = ToolSpec("noop", lambda params: {"ok": True})
            result = await ExecutionBackend(
                BoundedReAct(max_turns=6),
                (tool,),
                ExecutionProfile("fixed", ("noop",)),
                client,
                Path(temp),
            ).run(AgentTask("reset", "Return a result"), run_id="reset")
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(client.requests), 4)
            self.assertEqual(result.usage["leaf_calls"], 1)

    def test_protocol_limit_requires_positive_integer_or_none(self):
        for value in (0, -1, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReActAgent(max_protocol_failures=value)
