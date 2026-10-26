"""Real edit rejection must produce a recoverable failure, never an unknown write."""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.agent import (
    AgentTask,
    ExecutionBackend,
    ExecutionProfile,
    PublicToolError,
    ReActAgent,
    ToolRejectedBeforeEffect,
)
from datasets.lift.full_tools import FullLocalTools
from tests.support.agent_runtime import RecordedClient


class WritePreconditionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_fragment_is_receipted_and_agent_can_correct_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            file = workspace / "report.txt"
            file.write_bytes(b"Original report\r\n")
            original = hashlib.sha256(file.read_bytes()).hexdigest()
            args = {
                "path": "report.txt",
                "expected_sha256": original,
                "old": "missing",
                "new": "replacement",
            }
            corrected = {**args, "old": "Original", "new": "Corrected"}
            client = RecordedClient(
                [
                    {"tool": "replace_text", "params": args},
                    {"tool": "replace_text", "params": corrected},
                    {"final_response": "done", "artifacts": []},
                ]
            )
            tools = FullLocalTools(workspace).specs()
            backend = ExecutionBackend(
                ReActAgent(),
                tools,
                ExecutionProfile("fixed", tuple(t.name for t in tools)),
                client,
                root / "runs",
            )
            result = await backend.run(AgentTask("edit", "Correct the report"), run_id="recover")
            self.assertEqual(result.status, "completed")
            self.assertEqual(file.read_bytes(), b"Corrected report\r\n")
            self.assertEqual(result.usage["leaf_calls"], 2)
            self.assertEqual(result.usage["unknown_calls"], 0)
            self.assertEqual(result.usage["failed_leaf_calls"], 1)
            before = {p.name: p.read_bytes() for p in (root / "runs/recover/steps").glob("*.json")}
            replay = await backend.run(AgentTask("edit", "Correct the report"), run_id="recover")
            self.assertEqual(replay.to_dict(), result.to_dict())
            self.assertEqual(
                before,
                {p.name: p.read_bytes() for p in (root / "runs/recover/steps").glob("*.json")},
            )
            self.assertEqual(file.read_bytes(), b"Corrected report\r\n")

    async def test_write_followed_by_public_error_stays_unknown_and_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            file = workspace / "report.txt"
            file.write_text("original")

            class AfterWriteError(PublicToolError):
                public_codes = frozenset({"known_public_code"})

            def uncertain(params):
                file.write_text("effect happened")
                raise AfterWriteError("known_public_code")

            tools = tuple(
                replace(t, handler=uncertain) if t.name == "replace_text" else t
                for t in FullLocalTools(workspace).specs()
            )
            client = RecordedClient(
                [
                    {
                        "tool": "replace_text",
                        "params": {
                            "path": "report.txt",
                            "expected_sha256": "0" * 64,
                            "old": "original",
                            "new": "changed",
                        },
                    }
                ]
            )
            backend = ExecutionBackend(
                ReActAgent(),
                tools,
                ExecutionProfile("fixed", tuple(t.name for t in tools)),
                client,
                root / "runs",
            )
            task = AgentTask("uncertain", "Edit the report")
            first = await backend.run(task, run_id="uncertain")
            self.assertEqual(first.status, "unknown")
            self.assertEqual(file.read_text(), "effect happened")
            file.write_text("external sentinel")
            again = await backend.run(task, run_id="uncertain")
            self.assertEqual(again.status, "unknown")
            self.assertEqual(file.read_text(), "external sentinel")
            self.assertEqual(again.usage["unknown_calls"], 1)

    async def test_unrecognized_rejection_code_does_not_settle_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()

            def malformed(params):
                raise ToolRejectedBeforeEffect("undeclared")

            tools = tuple(
                replace(t, handler=malformed) if t.name == "replace_text" else t
                for t in FullLocalTools(workspace).specs()
            )
            client = RecordedClient(
                [
                    {
                        "tool": "replace_text",
                        "params": {
                            "path": "report.txt",
                            "expected_sha256": "0" * 64,
                            "old": "old",
                            "new": "new",
                        },
                    }
                ]
            )
            backend = ExecutionBackend(
                ReActAgent(),
                tools,
                ExecutionProfile("fixed", tuple(t.name for t in tools)),
                client,
                root / "runs",
            )
            result = await backend.run(AgentTask("bad-code", "Edit report"), run_id="bad-code")
            self.assertEqual(result.status, "unknown")
            self.assertEqual(result.usage["unknown_calls"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
