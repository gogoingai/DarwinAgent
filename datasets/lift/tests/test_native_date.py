"""Native evaluation calendar replay across midnight, with no provider calls."""

import asyncio
import importlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.native_date import RecordedJudgeClient

try:
    native = importlib.import_module("src.lift.eval.run_task")
except ModuleNotFoundError as exc:
    if exc.name not in {"src", "src.lift"}:
        raise
    native = None


@unittest.skipIf(native is None, "Optional pinned LIFT upstream is not configured")
class NativeDateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.day = "2026-10-14"
        case = self

        class Clock:
            @classmethod
            def now(cls):
                return datetime.fromisoformat(case.day)

        self.clock = patch.object(native, "datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.identity = {"run_id": "offline", "task_id": "q1", "kernel_id": None, "cell_id": "cell"}

    @contextmanager
    def scope(self, root, identity=None):
        try:
            module = importlib.import_module("datasets.lift.native_date")
        except ModuleNotFoundError:
            yield
            return
        with patch.object(module, "today", lambda: self.day):
            with module.native_date_scope(root, identity or self.identity):
                yield

    def prompt(self):
        return native._build_judge_prompt("public", "saved answer", "private rules")

    async def test_cross_midnight_replay_preserves_full_prompt_and_calendar_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.scope(root):
                first = self.prompt()
            path = root / "native-date.json"
            self.assertTrue(path.exists())
            raw = path.read_bytes()
            self.day = "2026-10-15"
            with self.scope(root):
                self.assertEqual(self.prompt(), first)
            self.assertEqual(path.read_bytes(), raw)
            self.assertIn("今天是 2026-10-15", self.prompt())

    async def test_calendar_tampering_or_changed_task_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.scope(root):
                pass
            with self.assertRaises(ValueError):
                with self.scope(root, dict(self.identity, task_id="q2")):
                    pass
            p = root / "native-date.json"
            row = json.loads(p.read_text())
            row["date"] = "2026-10-16"
            p.write_text(json.dumps(row))
            with self.assertRaises(ValueError):
                with self.scope(root):
                    pass

    async def test_legacy_single_date_is_recovered_without_rewriting_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "judge/chat.json"
            p.parent.mkdir()
            old = self.prompt()
            p.write_text(json.dumps({"history": [{"role": "user", "content": old}]}))
            raw = p.read_bytes()
            self.day = "2026-10-16"
            with self.scope(root):
                self.assertEqual(self.prompt(), old)
            self.assertEqual(p.read_bytes(), raw)

    async def test_mixed_legacy_dates_fail_closed_without_calendar_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "judge/chat.json"
            p.parent.mkdir()
            first = self.prompt()
            self.day = "2026-10-15"
            second = self.prompt()
            p.write_text(
                json.dumps(
                    {
                        "history": [
                            {"role": "user", "content": first},
                            {"role": "user", "content": second},
                        ]
                    }
                )
            )
            with self.assertRaises(ValueError):
                with self.scope(root):
                    pass
            self.assertFalse((root / "native-date.json").exists())

    async def test_concurrent_scopes_and_exceptions_do_not_leak_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            with self.scope(a):
                expected_a = self.prompt()
            self.day = "2026-10-15"
            with self.scope(b):
                expected_b = self.prompt()
            self.day = "2026-10-16"

            async def exercise(root, expected):
                with self.scope(root):
                    await asyncio.sleep(0)
                    self.assertEqual(self.prompt(), expected)

            await asyncio.gather(exercise(a, expected_a), exercise(b, expected_b))
            with self.assertRaises(RuntimeError):
                with self.scope(a):
                    raise RuntimeError("controlled")
            self.assertIn("今天是 2026-10-16", self.prompt())

    async def test_actual_judge_chat_replay_after_midnight_never_resends(self):
        from darwinagent.agent import AgentTask
        from datasets.lift.full_agents import FullDarwinChatAgent
        from datasets.lift.full_tools import FullLocalTools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            client = RecordedJudgeClient()
            shared = SimpleNamespace(
                worker_limit=None, scoped=lambda *args: client, check=lambda: None
            )

            def agent():
                return FullDarwinChatAgent(
                    AgentTask("q1", "public"),
                    FullLocalTools(workspace, readonly=True).specs(),
                    shared,
                    root / "judge",
                    None,
                    role="judge",
                    run_id="offline",
                    session="judge-offline",
                )

            with self.scope(root):
                first = await agent().chat(self.prompt(), session_id="judge-offline")
            self.day = "2026-10-15"
            with self.scope(root):
                second = await agent().chat(self.prompt(), session_id="judge-offline")
            self.assertEqual(first, second)
            self.assertEqual(client.calls, 1)
