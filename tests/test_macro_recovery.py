"""Known macro failures remain recoverable without hiding primitive effects."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.agent import (
    AgentContext,
    AgentTask,
    CheckRejected,
    ExecutionBackend,
    ExecutionProfile,
    ReActAgent,
    ToolFailure,
    ToolSpec,
    file_tools,
)
from darwinagent.operators.sandbox import Limits, SandboxError
from darwinagent.runtime.steps import StepJournal, UnknownRequest
from tests.support.agent_runtime import RecordedClient
from tests.support.macro_recovery import OVERSIZED_AFTER_WRITE, macro_bundle


class MacroRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = file_tools(self.root / "files")
        self.profile = ExecutionProfile("macro-recovery", tuple(t.name for t in self.tools))
        self.task = AgentTask("macro", "Complete the task")
        (self.root / "files/one.txt").write_text("a" * 100000)
        (self.root / "files/two.txt").write_text("b" * 100000)

    def context(self, code, *, journal=None, tools=None, limits=Limits()):
        tools = tools or self.tools
        profile = replace(self.profile, capabilities=tuple(t.name for t in tools))
        kernel = macro_bundle(
            self.root / ("kernel-" + str(len(list(self.root.glob("kernel-*"))))), profile, code
        )
        return AgentContext(
            self.task,
            profile,
            {t.name: t for t in tools},
            None,
            journal or StepJournal(self.root / "episode/steps"),
            kernel,
            limits,
        )

    async def test_react_recovers_and_counts_every_leaf(self):
        client = RecordedClient([{"tool": "combine", "params": {}}, {"final_response": "done"}])
        kernel = macro_bundle(self.root / "kernel", self.profile, OVERSIZED_AFTER_WRITE)
        backend = ExecutionBackend(
            ReActAgent(max_turns=3), self.tools, self.profile, client, self.root / "runs"
        )
        result = await backend.run(self.task, kernel, run_id="large")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.usage["leaf_calls"], 3)
        self.assertEqual(result.usage["failed_leaf_calls"], 0)
        failure = next(e for e in result.events if e["kind"] == "macro_failure")
        self.assertEqual(failure["error_code"], "result_byte_limit")
        self.assertEqual(len(failure["completed_receipts"]), 3)
        self.assertEqual(failure["failure"]["completed_receipts"], failure["completed_receipts"])
        self.assertEqual(failure["failure"]["error_code"], "result_byte_limit")
        self.assertEqual((self.root / "files/saved.txt").read_text(), "already done")
        self.assertEqual(client.calls, 2)
        self.assertEqual(
            (await backend.run(self.task, kernel, run_id="large")).to_dict(), result.to_dict()
        )
        self.assertEqual(client.calls, 2)

    async def test_replay_does_not_repeat_completed_write(self):
        context = self.context(OVERSIZED_AFTER_WRITE)
        with self.assertRaises(ToolFailure) as caught:
            await context.call("combine", {})
        self.assertIsInstance(caught.exception, SandboxError)
        receipt = self.root / "episode/steps/000000.json"
        before = receipt.read_bytes()
        stamp = (self.root / "files/saved.txt").stat().st_mtime_ns
        replay = self.context(
            OVERSIZED_AFTER_WRITE, journal=StepJournal(self.root / "episode/steps")
        )
        with self.assertRaises(ToolFailure):
            await replay.call("combine", {})
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual((self.root / "files/saved.txt").stat().st_mtime_ns, stamp)
        self.assertEqual(replay.usage()["leaf_calls"], 3)

    async def test_unknown_write_is_not_recoverable(self):
        calls = []

        def uncertain(params):
            calls.append(params)
            raise ConnectionError("secret host details")

        tool = ToolSpec("uncertain", uncertain, effect="write", description="uncertain write")
        context = self.context(
            'def run(params):\n    return call("uncertain", {})\n', tools=(tool,)
        )
        with self.assertRaises(UnknownRequest):
            await context.call("combine", {})
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(e["kind"] == "macro_failure" for e in context.events))

    async def test_timeout_with_unreceipted_write_stays_unknown(self):
        async def slow(params):
            await asyncio.sleep(1)
            return {}

        tool = ToolSpec("slow", slow, effect="write", description="slow write")
        context = self.context(
            'def run(params):\n    return call("slow", {})\n',
            tools=(tool,),
            limits=Limits(timeout_s=0.01),
        )
        with self.assertRaises(UnknownRequest):
            await context.call("combine", {})
        self.assertEqual(context.usage()["unknown_calls"], 1)
        self.assertFalse(any(e["kind"] == "macro_failure" for e in context.events))

    async def test_known_error_does_not_expose_host_detail(self):
        context = self.context('def run(params):\n    return params["private-token-value"]\n')
        with self.assertRaises(ToolFailure) as caught:
            await context.call("combine", {})
        self.assertNotIn("private-token-value", json.dumps(caught.exception.to_dict()))

    async def test_check_rejection_stops_macro_without_reclassifying_failure(self):
        context = self.context(OVERSIZED_AFTER_WRITE)

        def reject(stage, candidate):
            return not (stage == "after_action" and candidate.get("name") == "write_file")

        context.profile = replace(context.profile, host_checks=(reject,))
        with self.assertRaises(CheckRejected):
            await context.call("combine", {})
        self.assertTrue((self.root / "files/saved.txt").exists())
        self.assertEqual(context.usage()["leaf_calls"], 1)
        self.assertFalse(any(e["kind"] == "macro_failure" for e in context.events))

    async def test_cancelled_write_remains_submitted(self):
        async def interrupted(params):
            raise asyncio.CancelledError

        tool = ToolSpec("interrupted", interrupted, effect="write")
        context = self.context(
            'def run(params):\n    return call("interrupted", {})\n', tools=(tool,)
        )
        with self.assertRaises(asyncio.CancelledError):
            await context.call("combine", {})
        self.assertEqual(context.usage()["unknown_calls"], 1)
        self.assertFalse(any(e["kind"] == "macro_failure" for e in context.events))

    async def test_checker_sandbox_fault_is_not_a_recoverable_macro_failure(self):
        context = self.context(OVERSIZED_AFTER_WRITE)

        def broken_check(stage, candidate):
            if stage == "after_action":
                raise SandboxError("checker execution failed")
            return True

        context.profile = replace(context.profile, host_checks=(broken_check,))
        with self.assertRaises(SandboxError) as caught:
            await context.call("combine", {})
        self.assertNotIsInstance(caught.exception, ToolFailure)
        self.assertEqual(context.usage()["leaf_calls"], 1)
        self.assertFalse(any(e["kind"] == "macro_failure" for e in context.events))

    async def test_view_fault_is_not_a_recoverable_macro_failure(self):
        context = self.context("def run(params):\n    return view()\n")

        def broken_view():
            raise ValueError("invalid graph schema")

        context.view = broken_view
        with self.assertRaisesRegex(ValueError, "invalid graph schema") as caught:
            await context.call("combine", {})
        self.assertNotIsInstance(caught.exception, ToolFailure)
        self.assertFalse(any(e["kind"] == "macro_failure" for e in context.events))
