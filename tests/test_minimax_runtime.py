"""Offline protocol and durable uncertainty coverage; no service dispatch."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from darwinagent.agent import ExecutionProfile
from darwinagent.config import Config
from darwinagent.llm.client import LLMClient
from darwinagent.runtime.diagnostics import exception_evidence
from datasets.lift.tests.test_transport import Recorded
from datasets.lift.transport import SharedTransport


class Stream:
    def __init__(self, *, incomplete=False, close_failure=False):
        self.incomplete, self.close_failure = incomplete, close_failure

    async def __aiter__(self):
        yield NS(
            model="MiniMax-M3.1-Flash-Preview",
            id="chatcmpl-123",
            usage=None,
            choices=[
                NS(
                    delta=NS(content='{"final_response":"ok"}', reasoning_content="abc"),
                    finish_reason="stop",
                )
            ],
        )
        if self.incomplete:
            raise TimeoutError("fake-key PRIVATE-PROMPT")
        yield NS(model=None, id=None, choices=[], usage=NS(prompt_tokens=2, completion_tokens=3))

    async def close(self):
        if self.close_failure:
            raise ConnectionError("fake-key PRIVATE-PROMPT")


class MiniMaxTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_request_and_complete_close_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                work_dir=Path(directory),
                api_key="fake-key",
                api_base_url="https://example.invalid/v1",
                model_strong="MiniMax-M3.1-Flash-Preview",
                max_retries=1,
                provider_request_policy="minimax-adaptive-v1",
                thinking_type="adaptive",
                reasoning_effort="max",
            )
            cfg.role_tiers["worker"] = "strong"
            client = LLMClient(cfg)
            captured = []

            async def create(**kwargs):
                captured.append(kwargs)
                return Stream(close_failure=True)

            client._site_for = lambda model: (
                NS(chat=NS(completions=NS(create=create))),
                __import__("asyncio").Semaphore(1),
            )
            result = await client.chat(
                role="worker",
                messages=[],
                max_tokens=16384,
                temperature=0,
                json_mode=True,
                durable=True,
            )
            self.assertEqual(result.content, '{"final_response":"ok"}')
            self.assertEqual(result.usage["stream_close_error_type"], "ConnectionError")
            self.assertEqual(
                set(captured[0]),
                {
                    "model",
                    "messages",
                    "temperature",
                    "max_completion_tokens",
                    "stream",
                    "stream_options",
                    "timeout",
                    "response_format",
                    "extra_body",
                },
            )
            self.assertEqual(captured[0]["max_completion_tokens"], 16384)
            self.assertEqual(
                captured[0]["extra_body"],
                {"thinking": {"type": "adaptive"}, "reasoning_effort": "max"},
            )
            await client.aclose()

    async def test_incomplete_stream_metadata_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(work_dir=Path(directory), model_strong="recorded", max_retries=1)
            cfg.role_tiers["worker"] = "strong"
            client = LLMClient(cfg)

            async def create(**kwargs):
                return Stream(incomplete=True)

            client._site_for = lambda model: (
                NS(chat=NS(completions=NS(create=create))),
                __import__("asyncio").Semaphore(1),
            )
            with self.assertRaises(TimeoutError) as caught:
                await client.chat(role="worker", messages=[], durable=True)
            evidence = exception_evidence(caught.exception)
            self.assertFalse(evidence["stream_complete"])
            self.assertEqual(evidence["chunks_received"], 1)
            self.assertNotIn("fake-key", json.dumps(evidence))
            self.assertNotIn("PRIVATE-PROMPT", json.dumps(evidence))
            await client.aclose()

    async def test_quiet_eof_without_terminal_finish_is_unknown(self):
        from darwinagent.llm.client import IncompleteStream

        class QuietStream(Stream):
            async def __aiter__(self):
                yield NS(
                    model=None,
                    id=None,
                    usage=None,
                    choices=[
                        NS(delta=NS(content="partial", reasoning_content=None), finish_reason=None)
                    ],
                )

        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                work_dir=Path(directory),
                model_strong="MiniMax-M3.1-Flash-Preview",
                provider_request_policy="minimax-adaptive-v1",
                thinking_type="adaptive",
                reasoning_effort="max",
            )
            cfg.role_tiers["worker"] = "strong"
            client = LLMClient(cfg)

            async def create(**kwargs):
                return QuietStream()

            client._site_for = lambda model: (
                NS(chat=NS(completions=NS(create=create))),
                __import__("asyncio").Semaphore(1),
            )
            with self.assertRaises(IncompleteStream) as caught:
                await client.chat(role="worker", messages=[], durable=True)
            self.assertFalse(exception_evidence(caught.exception)["stream_complete"])
            self.assertEqual(exception_evidence(caught.exception)["partial_content_chars"], 7)
            await client.aclose()

    async def test_unbounded_http_ledger_and_exception_body_safety(self):
        import httpx
        from openai import APIStatusError

        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(work_dir=Path(directory), max_http_requests=None, api_key="fake-key")
            first = LLMClient(cfg)
            for _ in range(20):
                await first._reserve_http_attempt(None)
            second = LLMClient(cfg)
            await second._reserve_http_attempt(None)
            self.assertEqual(second.http_attempts(), 21)
            error = APIStatusError(
                "fake-key PRIVATE-PROMPT",
                response=httpx.Response(
                    500, request=httpx.Request("POST", "https://example.invalid")
                ),
                body={"prompt": "PRIVATE-PROMPT", "key": "fake-key"},
            )
            first._sanitize_transport_error(error)
            self.assertNotIn("PRIVATE-PROMPT", repr(error))
            self.assertIsNone(error.body)
            self.assertEqual(exception_evidence(error)["http_status"], 500)
            await first.aclose()
            await second.aclose()

    async def test_unbounded_counter_persists_and_unknown_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = Recorded(root / "client")
            client.cfg.max_http_requests = None
            for _ in range(20):
                await (
                    SharedTransport(client, root / "shared", worker_limit=None)
                    .scoped("task", "worker")
                    .chat(role="worker", messages=[])
                )
            self.assertEqual(
                json.loads((root / "shared/worker-calls.json").read_text()), {"task": 20}
            )
            client.unknown = True
            with self.assertRaises(ConnectionError):
                await (
                    SharedTransport(client, root / "shared", worker_limit=None)
                    .scoped("task", "worker")
                    .chat(role="worker", messages=[])
                )
            row = next(
                json.loads(p.read_text())
                for p in (root / "shared/requests").glob("*.json")
                if json.loads(p.read_text())["status"] == "unknown"
            )
            self.assertEqual(row["diagnostics"]["error_type"], "ConnectionError")
            self.assertEqual(
                ExecutionProfile("unbounded", (), max_model_calls=None).declaration()[
                    "max_model_calls"
                ],
                None,
            )


class UnboundedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_loop_beyond16_and_receipt_resume(self):
        from darwinagent.agent import AgentTask, ExecutionBackend, ReActAgent
        from tests.support.agent_runtime import RecordedClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RecordedClient([{"bad": "format"}] * 20 + [{"final_response": "done"}])
            backend = ExecutionBackend(
                ReActAgent(max_turns=None),
                (),
                ExecutionProfile("uncapped", (), max_model_calls=None),
                client,
                root,
            )
            first = await backend.run(AgentTask("q", "deliver"), run_id="one")
            self.assertEqual(first.status, "completed")
            self.assertEqual(client.calls, 21)
            second = await backend.run(AgentTask("q", "deliver"), run_id="one")
            self.assertEqual(second.status, "completed")
            self.assertEqual(client.calls, 21)

    async def test_journal_unknown_diagnostic_is_safe_and_never_replayed(self):
        from darwinagent.runtime.journal_client import JournalClient
        from darwinagent.runtime.steps import StepJournal, UnknownRequest

        class Failure:
            calls = 0

            async def chat(self, **kwargs):
                self.calls += 1
                cause = ValueError("fake-key PRIVATE-PROMPT")
                raise TimeoutError("fake-key PRIVATE-PROMPT") from cause

        with tempfile.TemporaryDirectory() as directory:
            client = Failure()
            with self.assertRaises(UnknownRequest):
                await JournalClient(client, StepJournal(directory)).chat(role="worker", messages=[])
            text = (Path(directory) / "000000.json").read_text()
            self.assertIn('"TimeoutError"', text)
            self.assertNotIn("fake-key", text)
            self.assertNotIn("PRIVATE-PROMPT", text)
            with self.assertRaises(UnknownRequest):
                await JournalClient(client, StepJournal(directory)).chat(role="worker", messages=[])
            self.assertEqual(client.calls, 1)


class CompletedCancellationTests(unittest.IsolatedAsyncioTestCase):
    def client(self, root, stream, *, deadline=True):
        import asyncio
        import time

        cfg = Config(
            work_dir=root / "client",
            api_key="req-FAKESECRET123",
            max_http_requests=None,
            model_strong="MiniMax-M3.1-Flash-Preview",
            provider_request_policy="minimax-adaptive-v1",
            thinking_type="adaptive",
            reasoning_effort="max",
            deadline_monotonic=time.monotonic() + 0.08 if deadline else None,
        )
        cfg.role_tiers["worker"] = "strong"
        client = LLMClient(cfg)
        client.mock_calls = 0

        async def create(**kwargs):
            client.mock_calls += 1
            return stream

        client._site_for = lambda model: (
            NS(chat=NS(completions=NS(create=create))),
            asyncio.Semaphore(1),
        )
        return client

    async def test_deadline_during_complete_close_and_ledger_returns_known_receipt(self):
        import asyncio

        from darwinagent.runtime.journal_client import JournalClient
        from darwinagent.runtime.steps import StepJournal

        for stage in ("close", "ledger"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:

                class HungClose(Stream):
                    async def close(self):
                        await asyncio.Event().wait()

                root = Path(directory)
                client = self.client(root, HungClose() if stage == "close" else Stream())
                if stage == "ledger":

                    async def hung_ledger(*args, **kwargs):
                        await asyncio.Event().wait()

                    client._append_ledger = hung_ledger
                shared = SharedTransport(client, root / "shared", worker_limit=None)
                scope = shared.scoped("task", "worker")
                result = await JournalClient(scope, StepJournal(root / "steps")).chat(
                    role="worker", messages=[]
                )
                self.assertEqual(result.content, '{"final_response":"ok"}')
                key = "stream_close_error_type" if stage == "close" else "ledger_write_error_type"
                self.assertEqual(result.usage[key], "CancelledError")
                row = json.loads(next((root / "shared/requests").glob("*.json")).read_text())
                self.assertEqual(row["status"], "completed")
                self.assertFalse((root / "shared/pause.json").exists())
                await JournalClient(scope, StepJournal(root / "steps")).chat(
                    role="worker", messages=[]
                )
                self.assertEqual(client.mock_calls, 1)
                await client.aclose()

    async def test_external_cancel_preserves_completed_receipts_and_still_cancels_caller(self):
        import asyncio

        from darwinagent.runtime.journal_client import JournalClient
        from darwinagent.runtime.steps import StepJournal

        started = asyncio.Event()

        class HungClose(Stream):
            async def close(self):
                started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.client(root, HungClose(), deadline=False)
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            scope = shared.scoped("task", "worker")
            task = asyncio.create_task(
                JournalClient(scope, StepJournal(root / "steps")).chat(role="worker", messages=[])
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(
                json.loads((root / "steps/000000.json").read_text())["state"], "responded"
            )
            self.assertEqual(
                json.loads(next((root / "shared/requests").glob("*.json")).read_text())["status"],
                "completed",
            )
            await JournalClient(scope, StepJournal(root / "steps")).chat(role="worker", messages=[])
            self.assertEqual(client.mock_calls, 1)
            await client.aclose()

    async def test_incomplete_deadline_is_unknown_and_response_id_never_leaks(self):
        import asyncio

        from darwinagent.llm.client import BudgetExceeded

        class Partial(Stream):
            async def __aiter__(self):
                yield NS(
                    model=None,
                    id="req-FAKESECRET123",
                    usage=None,
                    choices=[
                        NS(
                            delta=NS(content="PRIVATE-PARTIAL", reasoning_content=None),
                            finish_reason=None,
                        )
                    ],
                )
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = self.client(root, Partial())
            shared = SharedTransport(client, root / "shared", worker_limit=None)
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("task", "worker").chat(role="worker", messages=[])
            text = next((root / "shared/requests").glob("*.json")).read_text()
            row = json.loads(text)
            self.assertEqual(row["status"], "unknown")
            self.assertFalse(row["diagnostics"]["stream_complete"])
            self.assertEqual(row["diagnostics"]["chunks_received"], 1)
            self.assertEqual(len(row["diagnostics"]["response_id_sha256"]), 64)
            self.assertNotIn("req-FAKESECRET123", text)
            self.assertNotIn("PRIVATE-PARTIAL", text)
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("another-task", "worker").chat(role="worker", messages=[])
            self.assertEqual(client.mock_calls, 1)
            await client.aclose()

    def test_config_old_positional_fields_keep_original_order(self):
        import dataclasses

        fields = [f for f in dataclasses.fields(Config) if f.init]
        self.assertEqual(fields[-1].name, "provider_request_policy")
        old_values = [getattr(Config(), f.name) for f in fields[:-1]]
        names = [f.name for f in fields[:-1]]
        replacements = {
            "request_timeout_s": 321.0,
            "max_http_requests": 123,
            "deadline_monotonic": 456.0,
            "request_budget_path": Path("legacy-counter.json"),
        }
        for name, value in replacements.items():
            old_values[names.index(name)] = value
        legacy = Config(*old_values)
        for name, value in replacements.items():
            self.assertEqual(getattr(legacy, name), value)
        self.assertEqual(legacy.provider_request_policy, "generic")
