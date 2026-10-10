"""Bounded batched actions preserve leaf checks, replay and uncertain effects."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, file_tools
from tests.support.agent_runtime import RecordedClient


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, root, client, *, tools=None, checks=()):
        tools = tools or file_tools(root / "files")
        return ExecutionBackend(
            ReActAgent(),
            tools,
            ExecutionProfile(
                "batch", tuple(t.name for t in tools), max_model_calls=8, host_checks=checks
            ),
            client,
            root / "receipts",
        )

    async def test_malformed_second_action_validates_whole_batch_before_writing(self):
        for second in (
            {"tool": "read_file", "params": {}},
            {"tool": "missing", "params": {}},
            {"final_response": "invalid"},
        ):
            with self.subTest(second=second), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                client = RecordedClient(
                    [
                        {
                            "actions": [
                                {"tool": "write_file", "params": {"path": "never", "content": "x"}},
                                second,
                            ]
                        },
                        {"final_response": "done"},
                    ]
                )
                result = await self.backend(root, client).run(
                    AgentTask("q", "deliver"), run_id="one"
                )
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.usage["leaf_calls"], 0)
                self.assertFalse((root / "files/never").exists())

    async def test_partial_known_failure_stops_remaining_and_replays_without_duplicate_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            client = RecordedClient(
                [
                    {
                        "actions": [
                            {"tool": "write_file", "params": {"path": "once", "content": "x"}},
                            {"tool": "read_file", "params": {"path": "missing"}},
                            {"tool": "write_file", "params": {"path": "never", "content": "x"}},
                        ]
                    },
                    {"final_response": "done"},
                ]
            )
            backend = self.backend(root, client)
            task = AgentTask("q", "deliver")
            from darwinagent.agent import react

            original = react._run_batch

            async def interrupted(context, actions):
                await original(context, actions)
                raise asyncio.CancelledError

            with (
                patch.object(react, "_run_batch", interrupted),
                self.assertRaises(asyncio.CancelledError),
            ):
                await backend.run(task, run_id="one")
            modified = (root / "files/once").stat().st_mtime_ns
            result = await backend.run(task, run_id="one")
            self.assertEqual(result.status, "completed")
            self.assertEqual(client.calls, 2)
            self.assertEqual(result.usage["leaf_calls"], 2)
            self.assertEqual((root / "files/once").stat().st_mtime_ns, modified)
            self.assertFalse((root / "files/never").exists())
            self.assertEqual(sum(e["kind"] == "tool_failure" for e in result.events), 1)

    async def test_unknown_after_effect_is_quarantined_and_never_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            read, write = file_tools(root / "files")
            calls = []

            async def uncertain(params):
                calls.append(params)
                await write.handler(params)
                raise ConnectionError("lost receipt after write")

            client = RecordedClient(
                [
                    {
                        "actions": [
                            {"tool": "write_file", "params": {"path": "once", "content": "x"}},
                            {"tool": "read_file", "params": {"path": "once"}},
                        ]
                    }
                ]
            )
            backend = self.backend(root, client, tools=[read, replace(write, handler=uncertain)])
            task = AgentTask("q", "deliver")
            first = await backend.run(task, run_id="one")
            modified = (root / "files/once").stat().st_mtime_ns
            self.assertEqual(first.status, "unknown")
            self.assertEqual((await backend.run(task, run_id="one")).status, "unknown")
            self.assertEqual(len(calls), 1)
            self.assertEqual(client.calls, 1)
            self.assertEqual((root / "files/once").stat().st_mtime_ns, modified)

    async def test_batch_leaf_accounting_and_checks_still_apply(self):
        def deny(stage, candidate):
            return not (stage == "before_action" and candidate.get("name") == "write_file")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "files").mkdir()
            (root / "files/input").write_text("x")
            client = RecordedClient(
                [
                    {
                        "actions": [
                            {"tool": "read_file", "params": {"path": "input"}},
                            {"tool": "read_file", "params": {"path": "input"}},
                        ]
                    },
                    {"final_response": "done"},
                ]
            )
            result = await self.backend(root, client).run(AgentTask("q", "deliver"), run_id="one")
            self.assertEqual(result.usage["model_calls"], 2)
            self.assertEqual(result.usage["leaf_calls"], 2)
            client = RecordedClient(
                [
                    {
                        "actions": [
                            {"tool": "write_file", "params": {"path": "denied", "content": "x"}},
                            {"tool": "read_file", "params": {"path": "input"}},
                        ]
                    },
                    {"final_response": "done"},
                ]
            )
            result = await self.backend(root, client, checks=(deny,)).run(
                AgentTask("q", "deliver"), run_id="two"
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.usage["leaf_calls"], 0)
            self.assertFalse((root / "files/denied").exists())
