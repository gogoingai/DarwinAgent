import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, ToolSpec
from tests.support.agent_runtime import RawRecordedClient
from tests.support.invalid_context import CompactingNativeReAct, CompactingReAct


class InvalidContextTests(unittest.IsolatedAsyncioTestCase):
    def test_context_limit_requires_positive_integer_or_none(self):
        for value in (0, -1, True, "2048"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CompactingReAct(invalid_reply_context_chars=value)

    @unittest.skipIf(
        CompactingNativeReAct is None, "Optional pinned LIFT upstream is not configured"
    )
    def test_context_configuration_changes_backend_identity(self):
        first = ExecutionBackend(
            CompactingNativeReAct(invalid_reply_context_chars=64), (), ExecutionProfile("fixed", ())
        )
        second = ExecutionBackend(
            CompactingNativeReAct(invalid_reply_context_chars=128),
            (),
            ExecutionProfile("fixed", ()),
        )
        self.assertNotEqual(first.identity(), second.identity())

    async def test_working_context_is_bounded_but_raw_reply_remains_complete(self):
        invalid = '{"tool":"write_text","params":{"content":"' + "x" * 40000
        client = RawRecordedClient([invalid, '{"final_response":"done"}'])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = await ExecutionBackend(
                CompactingReAct(max_turns=3),
                (),
                ExecutionProfile("fixed", ()),
                client,
                root,
            ).run(AgentTask("bad", "Produce a file"), run_id="bad")
            self.assertEqual(result.status, "completed")
            messages = client.requests[1]["messages"]
            assistant = next(m["content"] for m in messages if m["role"] == "assistant")
            self.assertLessEqual(len(assistant), 2048)
            steps = sorted((root / "bad/steps").glob("*.json"))
            self.assertEqual(json.loads(steps[0].read_text())["response"]["content"], invalid)
            self.assertEqual(result.usage["model_calls"], 2)

    async def test_valid_action_content_is_not_trimmed(self):
        content = "x" * 5000
        reply = json.dumps({"tool": "echo", "params": {"content": content}})
        client = RawRecordedClient([reply, '{"final_response":"done"}'])
        with tempfile.TemporaryDirectory() as temp:
            tool = ToolSpec("echo", lambda params: params["content"])
            result = await ExecutionBackend(
                CompactingReAct(max_turns=3),
                (tool,),
                ExecutionProfile("fixed", ("echo",)),
                client,
                Path(temp),
            ).run(AgentTask("valid", "Echo content"), run_id="valid")
            self.assertEqual(result.status, "completed")
            assistant = next(
                m["content"] for m in client.requests[1]["messages"] if m["role"] == "assistant"
            )
            self.assertEqual(assistant, reply)
            self.assertEqual(result.usage["leaf_calls"], 1)

    async def test_ordinary_react_keeps_legacy_invalid_context(self):
        invalid = "x" * 5000
        client = RawRecordedClient([invalid, '{"final_response":"done"}'])
        with tempfile.TemporaryDirectory() as temp:
            result = await ExecutionBackend(
                ReActAgent(max_turns=3),
                (),
                ExecutionProfile("fixed", ()),
                client,
                Path(temp),
            ).run(AgentTask("legacy", "Finish"), run_id="legacy")
            self.assertEqual(result.status, "completed")
            assistant = next(
                m["content"] for m in client.requests[1]["messages"] if m["role"] == "assistant"
            )
            self.assertEqual(assistant, invalid)
