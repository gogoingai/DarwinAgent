"""Known runtime feedback stays in the same bounded ReAct loop; unknowns stop it."""

import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, file_tools
from tests.support.agent_runtime import RawRecordedClient, RecordedClient


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

    async def test_settled_natural_language_feedback_recovers_without_repeating_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = "计算已验证。正在写入结果文件。"
            client = RawRecordedClient(
                [
                    malformed,
                    json.dumps(
                        {"tool": "write_file", "params": {"path": "report.txt", "content": "once"}}
                    ),
                    '{"tool":"write_file","params":',
                    json.dumps({"final_response": "valid"}),
                ]
            )
            backend = ExecutionBackend(
                ReActAgent(),
                file_tools(root / "files"),
                ExecutionProfile("protocol", ("read_file", "write_file")),
                client,
                root / "receipts",
            )
            task = AgentTask("q", "write a report")
            result = await backend.run(task, run_id="first")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.usage["model_calls"], 4)
            self.assertEqual(result.usage["leaf_calls"], 1)
            self.assertEqual(result.usage["unknown_calls"], 0)
            self.assertEqual((root / "files/report.txt").read_text(), "once")
            modified = (root / "files/report.txt").stat().st_mtime_ns
            self.assertTrue(all(r["json_mode"] is True for r in client.requests))
            messages = client.requests[1]["messages"]
            self.assertEqual(messages[-2], {"role": "assistant", "content": malformed})
            feedback = json.loads(messages[-1]["content"])
            self.assertEqual(feedback["observation"]["error"]["error_type"], "JSONDecodeError")
            self.assertIn("JSON", feedback["observation"]["error"]["message"])
            receipts = [
                json.loads(p.read_text()) for p in (root / "receipts/first/steps").glob("*.json")
            ]
            self.assertTrue(
                any(r.get("response", {}).get("content") == malformed for r in receipts)
            )
            replay = await backend.run(task, run_id="first")
            self.assertEqual(replay.to_dict(), result.to_dict())
            self.assertEqual(len(client.requests), 4)
            self.assertEqual((root / "files/report.txt").stat().st_mtime_ns, modified)

    async def test_malformed_replies_obey_existing_model_and_turn_caps(self):
        for turns, cap, expected_calls in [(16, 8, 8), (2, 8, 2)]:
            with self.subTest(turns=turns, cap=cap), tempfile.TemporaryDirectory() as temporary:
                client = RawRecordedClient(["not JSON"] * 9)
                backend = ExecutionBackend(
                    ReActAgent(max_turns=turns),
                    [],
                    ExecutionProfile("protocol-cap", (), max_model_calls=cap),
                    client,
                    Path(temporary),
                )
                result = await backend.run(AgentTask("q", "deliver"), run_id="first")
                self.assertEqual(result.status, "budget_exhausted")
                self.assertEqual(len(client.requests), expected_calls)
                self.assertEqual(result.usage["model_calls"], expected_calls)
                self.assertEqual(result.usage["leaf_calls"], 0)

    async def test_unknown_model_after_malformed_response_stays_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RawRecordedClient(["not JSON", ConnectionError("lost receipt")])
            backend = ExecutionBackend(
                ReActAgent(),
                [],
                ExecutionProfile("protocol-unknown", ()),
                client,
                root,
            )
            task = AgentTask("q", "deliver")
            first = await backend.run(task, run_id="first")
            self.assertEqual(first.status, "unknown")
            self.assertEqual(first.usage["model_calls"], 2)
            self.assertEqual(first.usage["unknown_calls"], 1)
            self.assertEqual((await backend.run(task, run_id="first")).status, "unknown")
            self.assertEqual(len(client.requests), 2)
            self.assertFalse((root / "first/result.json").exists())

    async def test_valid_json_with_unsupported_action_is_not_parse_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = RawRecordedClient(
                ['{"unsupported_action":true}', '{"final_response":"valid"}']
            )
            backend = ExecutionBackend(
                ReActAgent(),
                [],
                ExecutionProfile("unsupported", ()),
                client,
                Path(temporary),
            )
            result = await backend.run(AgentTask("q", "deliver"), run_id="first")
            self.assertEqual(result.status, "failed")
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(result.events[-1]["error_type"], "ValueError")
