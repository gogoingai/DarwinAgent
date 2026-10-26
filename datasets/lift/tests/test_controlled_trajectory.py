"""Control snapshots remain in verified native trajectories, including all model turns."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.agent import AgentContext, AgentTask, ExecutionProfile, ToolFailure
from darwinagent.runtime.steps import StepJournal
from datasets.lift.full_tools import FullLocalTools
from datasets.lift.task_control import TaskControl, TaskControlPolicy
from datasets.lift.transport import SharedTransport
from tests.support.full_lift_transport import FakeLLMClient
from tests.support.full_lift_web import page
from tests.support.macro_recovery import macro_bundle


class ControlledMacroTests(unittest.IsolatedAsyncioTestCase):
    async def test_macro_cannot_bypass_retrieval_stop_and_receipts_replay_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = SharedTransport(FakeLLMClient(root), root / "shared", worker_limit=None)
            tools = FullLocalTools(root / "workspace")
            tools.audit_path = root / "audit"
            tools.retrieval_control = TaskControl(
                shared, "worker", 24, TaskControlPolicy(retrieval_limit=2), tools.audit_path
            )
            specs = tools.specs()
            profile = ExecutionProfile("controlled", tuple(t.name for t in specs))
            code = 'def run(params):\n    call("read_web", {"url":"https://example.org/a"})\n    call("read_web", {"url":"https://example.org/b"})\n    call("read_web", {"url":"https://example.org/c"})\n    return {}\n'
            bundle = macro_bundle(root / "kernel", profile, code)

            def context():
                return AgentContext(
                    AgentTask("q", "research"),
                    profile,
                    {t.name: t for t in specs},
                    None,
                    StepJournal(root / "episode/steps"),
                    bundle,
                )

            with patch(
                "datasets.lift.full_tools.fetch_public", return_value=page("<p>evidence</p>")
            ) as fetch:
                first = context()
                with self.assertRaisesRegex(ToolFailure, "retrieval_limit_reached"):
                    await first.call("combine", {})
                self.assertEqual(first.usage()["leaf_calls"], 3)
                self.assertEqual(first.usage()["unknown_calls"], 0)
                self.assertEqual(fetch.call_count, 2)
                before = {p.name: p.read_bytes() for p in (root / "episode/steps").glob("*.json")}
                reopened = context()
                with self.assertRaisesRegex(ToolFailure, "retrieval_limit_reached"):
                    await reopened.call("combine", {})
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(
                    before,
                    {p.name: p.read_bytes() for p in (root / "episode/steps").glob("*.json")},
                )


class ControlledTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_model_steps_keep_both_task_control_snapshots_in_trajectory(self):
        from darwinagent.runtime.artifacts import atomic_json, digest
        from datasets.lift.full_trajectory import extract_trajectory
        from tests.support.full_lift_trajectory import recorded_transcript

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await recorded_transcript(root)
            # Add the new host snapshot exactly at the journal boundary, preserving
            # original local messages. Recompute fingerprints only in this fixture.
            expected = []
            for steps in sorted(root.rglob("steps")):
                for path in sorted(steps.glob("*.json")):
                    row = json.loads(path.read_text())
                    if row["kind"] != "model":
                        continue
                    snapshot = {
                        "execution_control": {
                            "policy": {"kind": "task-control-v1"},
                            "task_model": {
                                "used": len(expected),
                                "limit": 24,
                                "remaining": 24 - len(expected),
                            },
                        }
                    }
                    row["input"]["messages"].append(
                        {"role": "user", "content": json.dumps(snapshot)}
                    )
                    semantic = {
                        k: v
                        for k, v in row["input"].items()
                        if k not in {"temperature", "max_tokens"}
                    }
                    row["fingerprint"] = digest({"kind": "model", "input": semantic})
                    atomic_json(path, row)
                    expected.append(snapshot)
            trace = extract_trajectory(root, include_snapshots=False)
            snapshots = [
                json.loads(m["content"])
                for m in trace["all_messages"]
                if m.get("role") == "user"
                and m.get("content", "").startswith('{"execution_control"')
            ]
            self.assertEqual(snapshots, expected)
            self.assertGreaterEqual(len(snapshots), 2)
