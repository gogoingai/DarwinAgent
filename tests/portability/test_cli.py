"""Public CLI and real HTTP transport acceptance against a localhost fake endpoint."""

import asyncio
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from darwinagent import AnswerResult, Config, RunResult, SourceRef
from darwinagent.cli import main
from darwinagent.demo import MaintenanceEvaluator, ScriptedTransport
from darwinagent.llm.client import BudgetExceeded, LLMClient, TransportExhausted
from darwinagent.llm.registry import request_policy, resolve


@contextmanager
def endpoint(
    statuses=(), *, error_message="temporary", stream_chunks=1, chunk_delay=0, completion=None
):
    received = []
    remaining = list(statuses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, data))
            status = remaining.pop(0) if remaining else 200
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = {"error": {"message": error_message, "type": "server_error"}}
                self.wfile.write(json.dumps(body).encode())
                return
            messages = data["messages"]
            try:
                payload = json.loads(messages[1]["content"])
                if "base_version" in payload:
                    role = "proposal"
                elif "event" in payload:
                    role = "wiki_maintainer"
                elif "candidate" in payload:
                    role = "review"
                elif "previous_results" in payload:
                    role = "tools"
                elif "tool_results" in payload:
                    role = "answer"
                else:
                    role = "extraction"
                text = asyncio.run(ScriptedTransport().chat(role=role, messages=messages)).content
            except (IndexError, ValueError):
                text = "OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {
                "id": "fake",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": data["model"],
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            if completion is not None:
                chunk["choices"][0]["delta"]["content"] = completion
            try:
                for _ in range(stream_chunks):
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(chunk_delay)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class PublicCLI(unittest.TestCase):
    def cli(self, args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, env or {}, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def test_doctor_and_replay_without_credentials_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.cli(["doctor", "--output", str(root / "doctor")])[0], 0)
            args = ["demo", "--output", str(root / "demo"), "--rounds", "2"]
            status, out, err = self.cli(args)
            self.assertEqual(status, 0, err)
            self.assertIn("not measured model performance", out)
            summary = json.loads((root / "demo/demo-summary.json").read_text())["summary"]
            self.assertEqual([d["accepted"] for d in summary["rounds"]], [True, False])
            second = json.loads(
                (root / "demo/R2/optimization/attempt-0/proposal-call.json").read_text()
            )
            self.assertGreater(second["input"]["wiki"]["version"], 0)
            self.assertTrue(any(e["kind"] == "formal" for e in second["input"]["wiki"]["entries"]))
            wiki = (root / "demo/optimization/wiki.json").read_bytes()
            self.assertEqual(self.cli(args + ["--resume"])[0], 0)
            self.assertEqual(wiki, (root / "demo/optimization/wiki.json").read_bytes())
            self.assertEqual(self.cli(args)[0], 1)

    def test_cli_reads_local_dotenv_without_exporting_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "DARWINAGENT_API_KEY=private\nDARWINAGENT_BASE_URL=http://example/v1\nDARWINAGENT_MODEL=demo\n"
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                status, out, err = self.cli(["doctor", "--output", str(root / "runs")])
            finally:
                os.chdir(previous)
            self.assertEqual(status, 0, err)
            self.assertIn("configuration: ready", out)
            self.assertNotIn("private", out + err)

    def test_evaluator_is_output_based_not_stage_based(self):
        evaluator = MaintenanceEvaluator()
        scores = []
        for answer in ("wrong", "林维护", "林于2026-09-01维护"):
            result = RunResult(
                "any-stage",
                "i",
                "v",
                (AnswerResult("q1", "answered", answer, (SourceRef("record", "doc", "1"),)),),
                0,
            )
            scores.append(asyncio.run(evaluator.evaluate(result)).metrics["accuracy"])
        self.assertEqual(scores, [0, 0.5, 1])

    def test_live_missing_config_and_doctor_probe_fail_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, _out, err = self.cli(["demo", "--mode", "live", "--output", tmp])
            self.assertEqual(status, 1)
            self.assertIn("DARWINAGENT_BASE_URL", err)
            self.assertEqual(self.cli(["doctor", "--check-model", "--output", tmp])[0], 1)

    def test_live_full_controller_uses_single_fake_chat_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp, endpoint() as (url, requests):
            env = {
                "DARWINAGENT_API_KEY": "secret-test-key",
                "DARWINAGENT_BASE_URL": url,
                "DARWINAGENT_MODEL": "test-model",
            }
            status, out, err = self.cli(
                [
                    "demo",
                    "--mode",
                    "live",
                    "--rounds",
                    "2",
                    "--output",
                    tmp,
                    "--max-requests",
                    "40",
                    "--timeout",
                    "30",
                ],
                env,
            )
            self.assertEqual(status, 0, err)
            self.assertGreater(len(requests), 10)
            self.assertLessEqual(len(requests), 40)
            self.assertEqual({path for path, _ in requests}, {"/v1/chat/completions"})
            self.assertEqual({body["model"] for _, body in requests}, {"test-model"})
            self.assertNotIn("secret-test-key", out + err)
            self.assertEqual(
                json.loads((Path(tmp) / "http_attempts.json").read_text())["attempts"],
                len(requests),
            )
            summary = json.loads((Path(tmp) / "demo-summary.json").read_text())["summary"]
            self.assertEqual([d["accepted"] for d in summary["rounds"]], [True, False])

    def test_live_error_has_no_replay_fallback_and_whole_run_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "DARWINAGENT_API_KEY": "secret",
                "DARWINAGENT_BASE_URL": "http://127.0.0.1:1/v1",
                "DARWINAGENT_MODEL": "test",
            }
            before = time.monotonic()
            status, out, _err = self.cli(
                ["demo", "--mode", "live", "--output", tmp, "--timeout", "0.05"], env
            )
            self.assertEqual(status, 1)
            self.assertLess(time.monotonic() - before, 2)
            self.assertNotIn("Replay demonstrates", out)
            self.assertFalse((Path(tmp) / "demo-summary.json").exists())


class TransportLimits(unittest.TestCase):
    def test_auth_failure_redacts_console_and_all_persisted_demo_artifacts(self):
        sentinel = "sentinel-review-key-never-real"
        with (
            tempfile.TemporaryDirectory() as tmp,
            endpoint([401, 401, 401], error_message="Invalid API key provided: " + sentinel) as (
                url,
                received,
            ),
        ):
            root = Path(tmp)
            out, err = io.StringIO(), io.StringIO()
            env = {
                "DARWINAGENT_API_KEY": sentinel,
                "DARWINAGENT_BASE_URL": url,
                "DARWINAGENT_MODEL": "test",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                status = main(
                    ["demo", "--mode", "live", "--rounds", "0", "--output", tmp, "--timeout", "3"]
                )
            self.assertEqual(status, 1)
            self.assertNotIn(sentinel, out.getvalue() + err.getvalue())
            self.assertTrue((root / "demo-summary.json").is_file())
            artifacts = [p for p in root.rglob("*") if p.is_file()]
            self.assertGreater(len(artifacts), 10)
            for path in artifacts:
                self.assertNotIn(sentinel.encode(), path.read_bytes(), str(path))
            failure = (root / "B0/generation/maintenance-demo/graph.failure.json").read_text()
            self.assertIn("[redacted]", failure)
            self.assertIn("Error code: 401", failure)
            self.assertEqual(
                len(received), 2
            )  # B0 generation and Wiki attribution both fail safely.

    def test_error_metadata_and_retry_exhaustion_redact_all_configured_key_tiers(self):
        secrets = ("sentinel-strong-secret", "sentinel-middle-secret", "sentinel-fast-secret")

        async def perform(url, root):
            cfg = Config(
                api_base_url=url,
                api_key=secrets[0],
                middle_api_key=secrets[1],
                fast_api_key=secrets[2],
                model_strong="test",
                max_retries=2,
                work_dir=root,
            )
            async with LLMClient(cfg) as client:
                with patch(
                    "darwinagent.llm.client.asyncio.sleep",
                    new=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(),
                ):
                    with self.assertRaises(TransportExhausted) as caught:
                        await client.chat(
                            role="answer",
                            messages=[{"role": "user", "content": "OK"}],
                            use_cache=False,
                        )
                error = caught.exception
                self.assertEqual(error.cause_type, "InternalServerError")
                self.assertEqual(error.__cause__.status_code, 500)
                text = repr(error) + str(error) + repr(error.__cause__) + repr(error.__cause__.body)
                for secret in secrets:
                    self.assertNotIn(secret, text)
                self.assertIn("[redacted]", text)

        with (
            tempfile.TemporaryDirectory() as tmp,
            endpoint([500, 500], error_message="Provider echoed " + " ".join(secrets)) as (
                url,
                received,
            ),
        ):
            root = Path(tmp)
            asyncio.run(perform(url, root))
            self.assertEqual(len(received), 2)
            for path in root.rglob("*"):
                if path.is_file():
                    for secret in secrets:
                        self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_connection_failure_cause_redacted_without_changing_exception_type(self):
        import httpx
        import openai

        secret = "sentinel-connection-secret"

        async def perform(root):
            cfg = Config(api_key=secret, work_dir=root, max_retries=1)
            async with LLMClient(cfg) as client:
                cause = ValueError("Connection rejected " + secret)
                error = openai.APIConnectionError(
                    message="Could not connect " + secret,
                    request=httpx.Request("POST", "http://example/v1"),
                )
                error.__cause__ = cause

                class Completions:
                    async def create(self, **kwargs):
                        raise error

                from types import SimpleNamespace

                fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
                client._site_for = lambda _model: (fake, asyncio.Semaphore(1))
                with self.assertRaises(TransportExhausted) as caught:
                    await client.chat(role="answer", messages=[], use_cache=False)
                wrapped = caught.exception
                self.assertEqual(wrapped.cause_type, "APIConnectionError")
                self.assertNotIn(secret, repr(wrapped) + repr(error) + repr(error.__cause__))

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(perform(Path(tmp)))

    def test_successful_model_text_is_preserved(self):
        async def perform(url, root):
            cfg = Config(api_base_url=url, api_key="test", model_strong="test", work_dir=root)
            async with LLMClient(cfg) as client:
                result = await client.chat(
                    role="answer", messages=[{"role": "user", "content": "OK"}], use_cache=False
                )
                self.assertEqual(result.content, "test subject")

        with (
            tempfile.TemporaryDirectory() as tmp,
            endpoint(completion="test subject") as (url, _received),
        ):
            asyncio.run(perform(url, Path(tmp)))

    def test_absolute_deadline_interrupts_continuous_stream_and_prevents_retries(self):
        async def perform(url, root):
            cfg = Config(
                api_base_url=url,
                api_key="test",
                model_strong="test",
                max_retries=3,
                request_timeout_s=0.1,
                work_dir=root,
            )
            async with LLMClient(cfg) as client:
                cfg.deadline_monotonic = time.monotonic() + 0.1
                started = time.monotonic()
                with self.assertRaisesRegex(BudgetExceeded, "deadline exceeded"):
                    await client.chat(
                        role="answer", messages=[{"role": "user", "content": "OK"}], use_cache=False
                    )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.25)
                self.assertEqual(client.http_attempts(), 1)
                self.assertFalse(cfg.cache_dir.exists())
                self.assertEqual(client.ledger_summary()["total_calls"], 0)

        with (
            tempfile.TemporaryDirectory() as tmp,
            endpoint(stream_chunks=8, chunk_delay=0.035) as (url, received),
        ):
            asyncio.run(perform(url, Path(tmp)))
            self.assertEqual(len(received), 1)

    def test_absolute_deadline_includes_semaphore_wait_and_closes_active_stream(self):
        from types import SimpleNamespace

        async def perform(root):
            cfg = Config(api_key="test", work_dir=root, max_retries=1)
            async with LLMClient(cfg) as client:
                closed = []

                class Stream:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        await asyncio.sleep(0.035)
                        return SimpleNamespace(
                            choices=[SimpleNamespace(delta=SimpleNamespace(content="x"))],
                            usage=None,
                        )

                    async def close(self):
                        closed.append(True)

                class Completions:
                    async def create(self, **kwargs):
                        return Stream()

                fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
                sem = asyncio.Semaphore(0)
                client._site_for = lambda _model: (fake, sem)
                cfg.deadline_monotonic = time.monotonic() + 0.05
                with self.assertRaises(BudgetExceeded):
                    await client.chat(role="answer", messages=[], use_cache=False)
                self.assertEqual(closed, [])
                self.assertEqual(client.http_attempts(), 0)
                sem.release()
                cfg.deadline_monotonic = time.monotonic() + 0.05
                with self.assertRaises(BudgetExceeded):
                    await client.chat(role="answer", messages=[], use_cache=False)
                self.assertEqual(closed, [True])

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(perform(Path(tmp)))

    def test_doctor_wraps_whole_model_check_in_explicit_time_limit(self):
        from darwinagent.cli import _doctor

        real_timeout = asyncio.timeout
        limits = []

        def short_timeout(seconds):
            limits.append(seconds)
            return real_timeout(0.1)

        with (
            tempfile.TemporaryDirectory() as tmp,
            endpoint(stream_chunks=8, chunk_delay=0.035) as (url, received),
        ):
            env = {
                "DARWINAGENT_API_KEY": "test",
                "DARWINAGENT_BASE_URL": url,
                "DARWINAGENT_MODEL": "test",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("darwinagent.cli.asyncio.timeout", side_effect=short_timeout),
            ):
                with redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
                    asyncio.run(_doctor(True, Path(tmp)))
            self.assertEqual(limits[0], 30)
            self.assertEqual(len(received), 1)

    def test_generic_model_names_do_not_silently_enable_vendor_routing(self):
        cfg = Config(
            api_base_url="http://example/v1", api_key="k", model_strong="MiniMax-M3.1-Flash-Preview"
        )
        resolved = resolve(cfg.model_strong, cfg)
        self.assertEqual(resolved.base_url, cfg.api_base_url)
        self.assertEqual(request_policy(resolved, True), ({}, 0))

    def test_retry_attempts_are_counted_and_capped_before_http_dispatch(self):
        async def perform(url, root, cap):
            cfg = Config(
                api_base_url=url,
                api_key="test",
                model_strong="test",
                max_retries=2,
                max_http_requests=cap,
                work_dir=root,
            )
            async with LLMClient(cfg) as client:
                with patch(
                    "darwinagent.llm.client.asyncio.sleep",
                    new=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(),
                ):
                    try:
                        await client.chat(
                            role="answer",
                            messages=[{"role": "user", "content": "OK"}],
                            use_cache=False,
                        )
                    except Exception:
                        pass
                return client.http_attempts()

        for cap, expected in ((1, 1), (2, 2)):
            with (
                self.subTest(cap=cap),
                tempfile.TemporaryDirectory() as tmp,
                endpoint([500]) as (url, requests),
            ):
                count = asyncio.run(perform(url, Path(tmp), cap))
                self.assertEqual(count, expected)
                self.assertEqual(len(requests), expected)

    def test_expired_deadline_sends_no_http_request(self):
        async def perform(url, root):
            cfg = Config(
                api_base_url=url,
                api_key="test",
                model_strong="test",
                max_retries=1,
                deadline_monotonic=time.monotonic() - 1,
                work_dir=root,
            )
            async with LLMClient(cfg) as client:
                with self.assertRaises(Exception):
                    await client.chat(
                        role="answer", messages=[{"role": "user", "content": "OK"}], use_cache=False
                    )
                self.assertEqual(client.http_attempts(), 0)

        with tempfile.TemporaryDirectory() as tmp, endpoint() as (url, requests):
            asyncio.run(perform(url, Path(tmp)))
            self.assertEqual(requests, [])
