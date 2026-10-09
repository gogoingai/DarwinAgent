"""Settled local failures continue; unknown/global gates and history remain durable."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from darwinagent.agent import AgentTask, EpisodeResult
from darwinagent.config import Config
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.transport import SharedTransport

try:
    import src.lift  # noqa: F401
except ModuleNotFoundError:
    NATIVE = False
else:
    NATIVE = True


class Client:
    def __init__(self, root):
        self.cfg = Config(model_strong="recorded", max_http_requests=100, work_dir=root)
        self.calls = 0
        self.attempts = 0

    def http_attempts(self):
        return self.attempts

    async def chat(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(content="ok", usage={})


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_cap_allows_next_task_but_global_stops_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            client = Client(root)
            shared = SharedTransport(client, root / "shared")
            for _ in range(8):
                await shared.scoped("q1", "worker").chat()
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("q1", "worker").chat()
            await shared.scoped("q2", "worker").chat()
            self.assertEqual(client.calls, 9)
            self.assertFalse((shared.root / "pause.json").exists())
            client.attempts = 100
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("q5", "worker").chat()
            self.assertEqual(client.calls, 9)

    async def test_known_http_error_is_local_and_denied_send_is_not_unknown(self):
        import httpx
        from openai import APIStatusError

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            client = Client(root)
            shared = SharedTransport(client, root / "shared")
            response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"))
            client.chat = AsyncMock(
                side_effect=APIStatusError("invalid", response=response, body=None)
            )
            with self.assertRaises(APIStatusError):
                await shared.scoped("q1", "worker").chat()
            shared.check()
            records = [json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")]
            self.assertEqual(records[0]["status"], "failed")
            client.chat = AsyncMock(side_effect=BudgetExceeded("cap"))
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("q2", "worker").chat()
            records = [json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")]
            self.assertEqual({r["status"] for r in records}, {"failed", "budget_exhausted"})
            self.assertTrue(all(r["actual_http_attempts"] == 0 for r in records))

    def recovery_fixture(self, root):
        client = Client(root)
        client.attempts = 26
        shared = SharedTransport(client, root / "shared")
        raw = b'{"status": "budget_exhausted"}\n'
        (shared.root / "pause.json").write_bytes(raw)
        atomic_json(shared.root / "worker-calls.json", {"worker-one": 8})
        for i in range(26):
            atomic_json(
                shared.root / "requests" / f"{i}.json",
                {
                    "status": "completed",
                    "session": "worker-one" if i < 8 else "other",
                    "actual_http_attempts": 1,
                    "run_id": "historical",
                },
            )
        session = root / "historical/sessions/worker-one"
        atomic_json(
            session / "session-identity.json",
            {
                "session": "worker-one",
                "role": "worker",
                "run_id": "historical",
            },
        )
        atomic_json(
            session / "episodes/turn-1/steps/1.json",
            {
                "state": "prepared",
                "kind": "model",
                "pending_budget": "Cumulative worker session budget exhausted",
            },
        )
        return shared, session, raw

    def test_recovery_is_audited_idempotent_preserves_bytes_and_counters(self):
        with tempfile.TemporaryDirectory() as d:
            shared, session, raw = self.recovery_fixture(Path(d))
            before = (shared.root / "worker-calls.json").read_bytes()
            first = shared.recover_local_cap_pause(session_root=session, expected_http_attempts=26)
            shared.check()
            self.assertEqual(
                first,
                shared.recover_local_cap_pause(session_root=session, expected_http_attempts=26),
            )
            self.assertEqual((shared.root / "pause.json").read_bytes(), raw)
            self.assertEqual((shared.root / "recovery/original-pause.json").read_bytes(), raw)
            self.assertEqual((shared.root / "worker-calls.json").read_bytes(), before)
            self.assertEqual(shared.client.http_attempts(), 26)
            shared.pause("trace_incomplete")
            with self.assertRaisesRegex(BudgetExceeded, "trace_incomplete"):
                shared.check()

    def test_recovery_refuses_ambiguous_or_global_history(self):
        for fault in ("unknown", "trace", "global", "write", "pending", "mismatch"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as d:
                shared, session, _ = self.recovery_fixture(Path(d))
                if fault == "unknown":
                    atomic_json(shared.root / "requests/0.json", {"status": "unknown"})
                elif fault == "trace":
                    atomic_json(session / "trace-errors/1.json", {"error": "trace"})
                elif fault == "global":
                    shared.client.attempts = 100
                elif fault in {"write", "pending"}:
                    atomic_json(
                        session / "episodes/turn-1/steps/2.json",
                        {"state": "submitted" if fault == "write" else "prepared", "kind": "tool"},
                    )
                else:
                    atomic_json(shared.root / "worker-calls.json", {"worker-one": 7})
                with self.assertRaises(ValueError):
                    shared.recover_local_cap_pause(session_root=session, expected_http_attempts=26)
                self.assertFalse((shared.root / "recovery/local-cap.json").exists())


@unittest.skipUnless(NATIVE, "Pinned native environment required")
class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_failure_replays_without_dispatch_trace_or_duration_change(self):
        from datasets.lift.agents import DarwinChatAgent, LocalTaskFailure

        with tempfile.TemporaryDirectory() as d:
            agent = DarwinChatAgent.__new__(DarwinChatAgent)
            agent.root = Path(d)
            agent.task = AgentTask("q1", "query")
            agent.role, agent.session, agent.run_id = "worker", "one", "run"
            agent.cursor, agent.kernel, agent.trace = 0, None, False
            agent.shared = SimpleNamespace(check=lambda: None, pause=lambda status: None)
            agent.backend = SimpleNamespace(
                run=AsyncMock(
                    return_value=EpisodeResult(
                        "budget_exhausted",
                        "",
                        (),
                        (),
                        {"model_calls": 8, "leaf_calls": 7, "pending_calls": 0, "unknown_calls": 0},
                    )
                )
            )
            with patch("datasets.lift.agents.tracing.emit") as trace:
                with self.assertRaises(LocalTaskFailure):
                    await agent.chat("query", session_id="one")
                old = (agent.root / "turns/0.json").read_bytes()
                agent.cursor = 0
                with self.assertRaises(LocalTaskFailure):
                    await agent.chat("query", session_id="one")
                self.assertEqual((agent.root / "turns/0.json").read_bytes(), old)
                self.assertEqual(agent.backend.run.await_count, 1)
                self.assertEqual(trace.call_count, 1)

    async def test_trace_failure_quarantines_after_local_episode(self):
        from datasets.lift.agents import DarwinChatAgent

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shared = SharedTransport(Client(root), root / "shared")
            agent = DarwinChatAgent.__new__(DarwinChatAgent)
            agent.root, agent.task = root / "worker", AgentTask("q1", "query")
            agent.role, agent.session, agent.run_id = "worker", "one", "run"
            agent.cursor, agent.kernel, agent.trace, agent.shared = 0, None, True, shared
            agent.backend = SimpleNamespace(
                run=AsyncMock(return_value=EpisodeResult("failed", "", (), (), {}))
            )
            with patch("datasets.lift.agents.tracing.emit", side_effect=RuntimeError("trace")):
                with self.assertRaises(BudgetExceeded):
                    await agent.chat("query", session_id="one")
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("q2", "worker").chat()
            self.assertEqual(shared.client.calls, 0)
