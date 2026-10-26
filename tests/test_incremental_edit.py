import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile
from datasets.lift import text_edits
from datasets.lift.full_tools import FullLocalTools
from tests.support.agent_runtime import RawRecordedClient

try:
    from datasets.lift.full_agents import FullNativeReAct
except ModuleNotFoundError as exc:
    if exc.name not in {"src", "src.lift"}:
        raise
    NATIVE = False
else:
    NATIVE = True


@unittest.skipUnless(NATIVE, "Optional pinned LIFT upstream is not configured")
class IncrementalEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_edit_preserves_large_file_and_replays_without_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            path = workspace / "report.html"
            original = "标题\r\n" + "x" * 40000 + "<p>wrong</p>" + "y" * 40000
            path.write_bytes(original.encode())
            tools = FullLocalTools(workspace)
            read = await tools.read_text({"path": "report.html"})
            self.assertEqual(read.get("sha256"), hashlib.sha256(path.read_bytes()).hexdigest())
            params = dict(
                path="report.html",
                expected_sha256=read["sha256"],
                old="<p>wrong</p>",
                new="<p>correct</p>",
            )
            client = RawRecordedClient(
                [
                    json.dumps({"tool": "replace_text", "params": params}),
                    '{"final_response":"done"}',
                ]
            )
            specs = tools.specs()
            backend = ExecutionBackend(
                FullNativeReAct(max_turns=3),
                specs,
                ExecutionProfile("fixed", tuple(t.name for t in specs)),
                client,
                root / "runs",
            )
            result = await backend.run(AgentTask("edit", "Fix paragraph"), run_id="edit")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.usage["leaf_calls"], 1)
            self.assertEqual(path.read_bytes(), original.replace("wrong", "correct").encode())
            audit = sorted(tools.audit_path.glob("*.json"))
            before = {p.name: p.read_bytes() for p in audit}
            replay = await backend.run(AgentTask("edit", "Fix paragraph"), run_id="edit")
            self.assertEqual(replay.status, "completed")
            self.assertEqual(
                {p.name: p.read_bytes() for p in tools.audit_path.glob("*.json")}, before
            )
            self.assertEqual(len(client.requests), 2)

    async def test_stale_hash_rejects_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.txt"
            path.write_text("changed")
            tools = FullLocalTools(temp)
            with self.assertRaises(ValueError) as rejected:
                await tools.invoke(
                    "replace_text",
                    dict(path="a.txt", expected_sha256="0" * 64, old="changed", new="bad"),
                )
            self.assertEqual(path.read_text(), "changed")
            self.assertEqual(str(rejected.exception), "edit_stale_file")

    async def test_ambiguous_match_rejects_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.txt"
            path.write_text("same same")
            tools = FullLocalTools(temp)
            with self.assertRaises(ValueError) as rejected:
                await tools.invoke(
                    "replace_text",
                    dict(
                        path="a.txt",
                        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        old="same",
                        new="bad",
                    ),
                )
            self.assertEqual(path.read_text(), "same same")
            self.assertEqual(str(rejected.exception), "edit_match_not_unique")

    async def test_changed_edit_source_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            path = workspace / "a.txt"
            path.write_text("old")
            tools = FullLocalTools(workspace)
            source = root / "text_edits.py"
            source.write_bytes(Path(text_edits.__file__).read_bytes() + b"\n# changed\n")
            tools.edit_source = source
            with self.assertRaises(ValueError):
                await tools.invoke(
                    "replace_text",
                    dict(
                        path="a.txt",
                        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        old="old",
                        new="new",
                    ),
                )
            self.assertEqual(path.read_text(), "old")

    async def test_overlapping_matches_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.txt"
            path.write_text("aaa")
            tools = FullLocalTools(temp)
            with self.assertRaises(ValueError) as rejected:
                await tools.invoke(
                    "replace_text",
                    dict(
                        path="a.txt",
                        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        old="aa",
                        new="b",
                    ),
                )
            self.assertEqual(str(rejected.exception), "edit_match_not_unique")
            self.assertEqual(path.read_text(), "aaa")

    async def test_outside_path_and_symlink_cannot_modify_other_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "other.txt"
            outside.write_text("old")
            tools = FullLocalTools(workspace)
            params = dict(
                expected_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                old="old",
                new="new",
            )
            with self.assertRaises(ValueError) as rejected:
                await tools.invoke("replace_text", dict(params, path="../other.txt"))
            self.assertEqual(str(rejected.exception), "workspace_path_rejected")
            (workspace / "link.txt").symlink_to(outside)
            with self.assertRaises(ValueError) as rejected:
                await tools.invoke("replace_text", dict(params, path="link.txt"))
            self.assertEqual(str(rejected.exception), "workspace_path_rejected")
            self.assertEqual(outside.read_text(), "old")

    async def test_readonly_binding_never_exposes_or_dispatches_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.txt"
            path.write_text("old")
            tools = FullLocalTools(temp, readonly=True)
            self.assertNotIn("replace_text", [spec.name for spec in tools.specs()])
            with self.assertRaises(Exception) as rejected:
                await tools.invoke(
                    "replace_text",
                    dict(
                        path="a.txt",
                        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        old="old",
                        new="new",
                    ),
                )
            self.assertIsInstance(rejected.exception, PermissionError)
            self.assertEqual(path.read_text(), "old")
