"""Durable reassessment of known invalid scalar outputs, without task replay."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from darwinagent.llm.client import LLMClient
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.steps import UnknownRequest
from datasets.lift import full_trajectory
from tests.support.judge_retry import offline_transport as build_offline_transport
from tests.support.judge_retry import recorded_scalar_shared


def offline_transport(root, outputs, *, minimax=False):
    def factory(cfg, http_client):
        with patch("darwinagent.llm.client.httpx.AsyncClient", new=http_client):
            return LLMClient(cfg)

    return build_offline_transport(root, outputs, minimax=minimax, client_factory=factory)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def envelope(self):
        transcript = {"source_hashes": {}}
        transcript["sha256"] = digest(transcript)
        value = dict(
            source_hashes={},
            transcript=transcript,
            task_id="s/q1",
            kernel_id="K",
            messages=[{"role": "user", "content": "saved trajectory"}],
        )
        return dict(value, sha256=digest(value))

    async def run_score(self, shared, root, value):
        scorer = getattr(
            full_trajectory, "score_trajectory_with_retries", full_trajectory.score_trajectory
        )
        return await scorer(shared, "session", root, value)

    shared = staticmethod(recorded_scalar_shared)

    async def test_saturated_empty_response_stops_once_and_replays_without_sending(self):
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared([])
            shared.generation_max_tokens = 16384
            shared.chat = AsyncMock(
                return_value=SimpleNamespace(
                    content="",
                    usage={
                        "prompt_tokens": 576010,
                        "completion_tokens": 16384,
                        "reasoning_chars": 59485,
                        "empty_content": True,
                    },
                )
            )
            value = self.envelope()
            result = await full_trajectory.score_trajectory_with_retries(
                shared, "session", tmp, value, stop_on_saturated_empty=True
            )
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(result["retry_stop_reason"], "empty_completion_at_cap")
            self.assertEqual(len(result["attempts"]), 1)
            self.assertEqual(shared.chat.await_count, 1)
            before = {p: p.read_bytes() for p in Path(tmp).rglob("*.json")}
            replay = await full_trajectory.score_trajectory_with_retries(
                shared, "session", tmp, value, stop_on_saturated_empty=True
            )
            self.assertEqual(replay, result)
            self.assertEqual(shared.chat.await_count, 1)
            self.assertEqual(before, {p: p.read_bytes() for p in Path(tmp).rglob("*.json")})

    async def test_empty_response_below_cap_can_still_use_bounded_retry(self):
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared([])
            shared.generation_max_tokens = 16384
            shared.chat = AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        content="", usage={"completion_tokens": 7, "empty_content": True}
                    ),
                    SimpleNamespace(
                        content='{"trajectory_score":1}', usage={"completion_tokens": 9}
                    ),
                ]
            )
            result = await full_trajectory.score_trajectory_with_retries(
                shared, "session", tmp, self.envelope(), stop_on_saturated_empty=True
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(shared.chat.await_count, 2)

    async def test_empty_then_valid_keeps_both_receipts_and_all_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(["", '{"trajectory_score":0.75}'])
            result = await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0.75)
            self.assertEqual(result["usage"], {"prompt_tokens": 20, "completion_tokens": 14})
            self.assertEqual(
                [a["status"] for a in result["attempts"]], ["invalid_response", "completed"]
            )
            self.assertEqual(shared.messages[0], shared.messages[1])
            before = {p: p.read_bytes() for p in Path(tmp).rglob("*.json")}
            self.assertEqual(await self.run_score(shared, tmp, self.envelope()), result)
            self.assertEqual(len(shared.messages), 2)
            self.assertEqual(before, {p: p.read_bytes() for p in Path(tmp).rglob("*.json")})

    async def test_invalid_outputs_stop_at_three_and_replay_without_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(["", "bad", "", '{"trajectory_score":1}'])
            result = await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(len(shared.messages), 3)
            self.assertEqual(result["usage"]["completion_tokens"], 21)
            self.assertEqual(await self.run_score(shared, tmp, self.envelope()), result)
            self.assertEqual(len(shared.messages), 3)

    async def test_unknown_second_attempt_stays_unknown_without_another_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(["", TimeoutError("private")])
            with self.assertRaises(TimeoutError):
                await self.run_score(shared, tmp, self.envelope())
            with self.assertRaises(UnknownRequest):
                await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(len(shared.messages), 2)
            self.assertNotIn("private", "".join(p.read_text() for p in Path(tmp).rglob("*.json")))

    async def test_cancel_after_known_completion_resumes_without_resending(self):
        cancelled = asyncio.CancelledError()
        cancelled.completed_result = SimpleNamespace(
            content='{"trajectory_score":0.75}', usage={"prompt_tokens": 10}
        )
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(["", cancelled])
            with self.assertRaises(asyncio.CancelledError):
                await self.run_score(shared, tmp, self.envelope())
            result = await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(shared.messages), 2)
            self.assertEqual(result["usage"]["prompt_tokens"], 20)

    async def test_tampered_attempt_refused_even_if_parent_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(["", '{"trajectory_score":0.75}'])
            await self.run_score(shared, tmp, self.envelope())
            attempt = Path(tmp) / "attempts/000/result.json"
            self.assertTrue(attempt.exists())
            row = json.loads(attempt.read_text())
            row["raw_response"] = "tampered"
            atomic_json(attempt, row)
            with self.assertRaises(ValueError):
                await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(len(shared.messages), 2)

    async def test_non_retryable_known_failure_does_not_resend(self):
        import httpx
        from openai import APIStatusError

        error = APIStatusError(
            "known",
            response=httpx.Response(
                400, request=httpx.Request("POST", "https://synthetic.invalid")
            ),
            body=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared([error])
            result = await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(shared.messages), 1)

    async def test_changed_input_is_refused_before_any_new_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(["", '{"trajectory_score":0.75}'])
            await self.run_score(shared, tmp, self.envelope())
            value = self.envelope()
            value["messages"][0]["content"] = "other"
            value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                await self.run_score(shared, tmp, value)
            self.assertEqual(len(shared.messages), 2)

    async def test_physical_ledger_counts_empty_and_valid_reassessment(self):

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, shared, requests = offline_transport(root, ["", '{"trajectory_score":0.75}'])
            try:
                result = await self.run_score(shared, root / "judge", self.envelope())
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["usage"]["prompt_tokens"], 20)
                self.assertEqual(result["usage"]["completion_tokens"], 14)
                self.assertEqual(client.http_attempts(), 2)
                receipts = [
                    json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                ]
                self.assertEqual([r["status"] for r in receipts], ["completed", "completed"])
                self.assertEqual(sum(r["actual_http_attempts"] for r in receipts), 2)
                from datasets.lift.full_report import trajectory_receipts_match

                ordered = sorted(receipts, key=lambda r: r["started"])
                self.assertTrue(trajectory_receipts_match(result, ordered, root / "judge"))
                self.assertFalse(trajectory_receipts_match(result, ordered[-1:], root / "judge"))
                await self.run_score(shared, root / "judge", self.envelope())
                self.assertEqual(len(requests), 2)
                self.assertEqual(client.http_attempts(), 2)
            finally:
                await client.aclose()

    async def test_new_retry_policy_is_bound_to_host_receipts_in_report(self):
        from datasets.lift.full_report import trajectory_receipts_match

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, shared, requests = offline_transport(root, ['{"trajectory_score":0.75}'])
            shared.generation_max_tokens = 16384
            try:
                result = await full_trajectory.score_trajectory_with_retries(
                    shared, "session", root / "judge", self.envelope(), stop_on_saturated_empty=True
                )
                receipts = [
                    json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                ]
                self.assertTrue(trajectory_receipts_match(result, receipts, root / "judge"))
                self.assertEqual(len(requests), 1)
                result["identity"]["completion_cap"] = 8
                self.assertFalse(trajectory_receipts_match(result, receipts, root / "judge"))
            finally:
                await client.aclose()

    async def test_interrupted_child_input_tampering_rejected_before_retry(self):
        cancelled = asyncio.CancelledError()
        cancelled.completed_result = SimpleNamespace(content="", usage={})
        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared([cancelled, '{"trajectory_score":0.75}'])
            with self.assertRaises(asyncio.CancelledError):
                await self.run_score(shared, tmp, self.envelope())
            atomic_json(Path(tmp) / "attempts/000/input.json", {"changed": True})
            with self.assertRaises(ValueError):
                await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(len(shared.messages), 1)

    async def test_crash_after_raw_valid_response_is_saved_never_resends_it(self):
        original_save = full_trajectory.atomic_json

        def crash_after_response(path, value):
            original_save(path, value)
            if (
                value.get("status") == "invalid_response"
                and value.get("raw_response") == '{"trajectory_score":0.75}'
            ):
                raise SystemExit("simulated hard crash before parsing")

        with tempfile.TemporaryDirectory() as tmp:
            shared = self.shared(['{"trajectory_score":0.75}'])
            with patch(
                "datasets.lift.full_trajectory.atomic_json", side_effect=crash_after_response
            ):
                with self.assertRaises(SystemExit):
                    await self.run_score(shared, tmp, self.envelope())
            child = Path(tmp) / "attempts/000/result.json"
            before = child.read_bytes()
            result = await self.run_score(shared, tmp, self.envelope())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0.75)
            self.assertEqual(len(shared.messages), 1)
            self.assertEqual(child.read_bytes(), before)
            self.assertEqual(await self.run_score(shared, tmp, self.envelope()), result)

    async def test_nine_physical_scalar_attempts_are_all_audited_and_host_bound(self):
        from unittest.mock import AsyncMock

        from datasets.lift.full_report import host_evidence, trajectory_receipts_match
        from datasets.lift.rate_control import RateLimitedSessionTransport
        from datasets.lift.retry_evidence import read_rate_audit, settled_completions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = [
                "worker done",
                "judge done",
                529,
                529,
                "",
                529,
                529,
                "",
                529,
                529,
                '{"trajectory_score":0.75}',
            ]
            client, shared, requests = offline_transport(root, outputs, minimax=True)
            shared.scoped = lambda session, role: RateLimitedSessionTransport(shared, session, role)
            directory = root / "episodes/cell"
            try:
                for role in ("worker", "judge"):
                    response = await shared.scoped(role, role).chat(
                        role=role, messages=[{"role": "user", "content": "saved"}], max_tokens=16384
                    )
                    event = {
                        "kind": "model",
                        "response_digest": digest(response.content),
                        "usage": response.usage,
                    }
                    atomic_json(
                        directory / role / "chat.json",
                        {"turns": [{"episode": {"events": [event]}}]},
                    )
                with patch("datasets.lift.rate_control.wait_cooldown", new=AsyncMock()):
                    result = await self.run_score(shared, directory / "trajectory", self.envelope())
                self.assertEqual(result["status"], "completed")
                rows = [
                    json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                ]
                scalar = sorted(
                    [r for r in rows if r["role"] == "trajectory_judge"], key=lambda r: r["started"]
                )
                self.assertEqual(len(scalar), 9)
                self.assertEqual(sum(r["actual_http_attempts"] for r in scalar), 9)
                self.assertEqual(client.http_attempts(), 11)
                self.assertEqual(result["usage"]["prompt_tokens"], 30)
                self.assertEqual(result["usage"]["completion_tokens"], 21)
                audit = read_rate_audit(shared.root)
                completions = settled_completions(scalar, audit)
                self.assertEqual(len(completions), 3)
                self.assertTrue(
                    trajectory_receipts_match(result, completions, directory / "trajectory")
                )
                row = {
                    "evidence_key": "cell",
                    "trajectory": result,
                    "native": {"work_session_id": "worker", "judge_session_id": "judge"},
                }
                owner = SimpleNamespace(verify=lambda: None, shared=shared, root=root)
                self.assertTrue(host_evidence(owner, [row], rows))
                await self.run_score(shared, directory / "trajectory", self.envelope())
                self.assertEqual(client.http_attempts(), 11)
            finally:
                await client.aclose()


class ServerRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_single_attempt_stops_on_known_rejection(self):
        from openai import APIStatusError

        from datasets.lift.rate_control import RateLimitedSessionTransport

        with tempfile.TemporaryDirectory() as tmp:
            client, shared, requests = offline_transport(Path(tmp), [529, '{"trajectory_score":1}'])
            try:
                transport = RateLimitedSessionTransport(
                    shared, "s", "trajectory_judge", max_attempts=1
                )
                with self.assertRaises(APIStatusError):
                    await transport.chat(role="trajectory_judge", messages=[])
                self.assertEqual(len(requests), 1)
                self.assertEqual(client.http_attempts(), 1)
                self.assertFalse((shared.root / "service-rate-pending.json").exists())
            finally:
                await client.aclose()

    async def test_confirmed_529_then_success_preserves_both_physical_receipts(self):
        from unittest.mock import AsyncMock

        from datasets.lift.rate_control import RateLimitedSessionTransport

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, shared, requests = offline_transport(root, [529, '{"trajectory_score":0.75}'])
            try:
                transport = RateLimitedSessionTransport(shared, "s", "trajectory_judge")
                with patch("datasets.lift.rate_control.wait_cooldown", new=AsyncMock()):
                    reply = await transport.chat(
                        role="trajectory_judge", messages=[{"role": "user", "content": "saved"}]
                    )
                self.assertIn("0.75", reply.content)
                rows = [
                    json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                ]
                self.assertEqual(sorted(r["status"] for r in rows), ["completed", "failed"])
                self.assertEqual(sum(r["actual_http_attempts"] for r in rows), 2)
                self.assertEqual(client.http_attempts(), 2)
                self.assertEqual(requests[0], requests[1])
                from datasets.lift.retry_evidence import read_rate_audit

                audit = read_rate_audit(shared.root)
                self.assertEqual(len(audit), 2)
            finally:
                await client.aclose()

    async def test_known_server_failures_are_bounded_and_nonretryable_stops_immediately(self):
        from unittest.mock import AsyncMock

        from openai import APIStatusError

        from datasets.lift.rate_control import RateLimitedSessionTransport

        for status, count in ((529, 3), (500, 3), (502, 3), (503, 3), (504, 3), (400, 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                client, shared, requests = offline_transport(Path(tmp), [status] * 4)
                try:
                    transport = RateLimitedSessionTransport(shared, "s", "trajectory_judge")
                    with patch("datasets.lift.rate_control.wait_cooldown", new=AsyncMock()):
                        with self.assertRaises(APIStatusError):
                            await transport.chat(
                                role="trajectory_judge",
                                messages=[{"role": "user", "content": "saved"}],
                            )
                    rows = [
                        json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                    ]
                    self.assertEqual(len(requests), count)
                    self.assertEqual(client.http_attempts(), count)
                    self.assertEqual(len(rows), count)
                    self.assertTrue(
                        all(
                            r["status"] == "failed" and r["actual_http_attempts"] == 1 for r in rows
                        )
                    )
                finally:
                    await client.aclose()

    async def test_read_timeout_remains_unknown_and_does_not_retry(self):
        import httpx

        from datasets.lift.rate_control import RateLimitedSessionTransport

        with tempfile.TemporaryDirectory() as tmp:
            client, shared, requests = offline_transport(
                Path(tmp), [httpx.ReadTimeout("offline timeout"), "never"]
            )
            try:
                transport = RateLimitedSessionTransport(shared, "s", "trajectory_judge")
                with self.assertRaises(Exception):
                    await transport.chat(
                        role="trajectory_judge", messages=[{"role": "user", "content": "saved"}]
                    )
                rows = [
                    json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                ]
                self.assertEqual([r["status"] for r in rows], ["unknown"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(client.http_attempts(), 1)
                self.assertTrue((shared.root / "service-rate-pending.json").exists())
            finally:
                await client.aclose()
