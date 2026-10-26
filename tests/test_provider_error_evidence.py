import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIStatusError

from darwinagent.config import Config
from darwinagent.llm.client import LLMClient
from darwinagent.runtime.diagnostics import exception_evidence


class ProviderErrorEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_category_survives_sanitization_without_body(self):
        with tempfile.TemporaryDirectory() as directory:
            client = LLMClient(Config(work_dir=Path(directory), api_key="fake-secret"))
            for body in (
                {"code": "context_length_exceeded"},
                {
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "fake-secret PRIVATE-PROMPT",
                    }
                },
            ):
                error = APIStatusError(
                    "fake-secret PRIVATE-PROMPT",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://example.invalid")
                    ),
                    body=body,
                )
                error.transport_diagnostics = {"stage": "request", "chunks_received": 0}
                client._sanitize_transport_error(error)
                self.assertIsNone(error.body)
                self.assertNotIn("PRIVATE-PROMPT", repr(error))
                self.assertEqual(
                    exception_evidence(error).get("provider_error_category"), "context_limit"
                )
                self.assertEqual(exception_evidence(error)["stage"], "request")
            await client.aclose()

    async def test_unrecognized_and_conflicting_codes_do_not_become_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            client = LLMClient(Config(work_dir=Path(directory), api_key="fake-secret"))
            for body in (
                {"code": "fake-secret"},
                {"error": {"code": "context_length_exceeded"}, "code": "unsupported_parameter"},
                {"code": ["context_length_exceeded"]},
            ):
                error = APIStatusError(
                    "PRIVATE-PROMPT",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://example.invalid")
                    ),
                    body=body,
                )
                client._sanitize_transport_error(error)
                self.assertIsNone(error.body)
                self.assertNotIn("provider_error_category", exception_evidence(error))
            await client.aclose()

    async def test_sdk_failure_path_keeps_category_and_sends_once(self):
        with tempfile.TemporaryDirectory() as directory:
            client = LLMClient(
                Config(
                    work_dir=Path(directory),
                    api_key="fake-secret",
                    model_strong="recorded",
                    max_retries=3,
                )
            )
            client.cfg.role_tiers["worker"] = "strong"
            calls = []

            async def create(**kwargs):
                calls.append(kwargs)
                raise APIStatusError(
                    "fake-secret PRIVATE-PROMPT",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://example.invalid")
                    ),
                    body={
                        "error": {
                            "code": "context_length_exceeded",
                            "message": "fake-secret PRIVATE-PROMPT",
                        }
                    },
                )

            client._site_for = lambda model: (
                SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                asyncio.Semaphore(1),
            )
            with self.assertRaises(APIStatusError) as caught:
                await client.chat(role="worker", messages=[], durable=True)
            evidence = exception_evidence(caught.exception)
            self.assertEqual(evidence["provider_error_category"], "context_limit")
            self.assertEqual(evidence["http_status"], 400)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("PRIVATE-PROMPT", str(evidence))
            self.assertIsNone(caught.exception.body)
            await client.aclose()

    async def test_minimax_fixed_signatures_survive_body_erasure(self):
        cases = (
            (
                {"error": {"message": "invalid params, context window exceeds limit (2013)"}},
                "context_limit",
            ),
            (
                {"base_resp": {"status_code": 2013, "status_msg": "PRIVATE-PROMPT"}},
                "invalid_parameters",
            ),
            ({"error": {"code": "1008", "message": "PRIVATE-PROMPT"}}, "insufficient_balance"),
            ({"base_resp": {"status_code": 1039}}, "token_limit"),
        )
        with tempfile.TemporaryDirectory() as directory:
            client = LLMClient(Config(work_dir=Path(directory), api_key="fake-secret"))
            for body, expected in cases:
                error = APIStatusError(
                    "PRIVATE-PROMPT",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://example.invalid")
                    ),
                    body=body,
                )
                client._sanitize_transport_error(error)
                self.assertIsNone(error.body)
                evidence = exception_evidence(error)
                self.assertEqual(evidence.get("provider_error_category"), expected)
                self.assertNotIn("PRIVATE-PROMPT", str(evidence))
            await client.aclose()

    async def test_reflected_or_conflicting_minimax_features_are_discarded(self):
        bodies = (
            {
                "error": {
                    "message": "PRIVATE-PROMPT invalid params, context window exceeds limit (2013)"
                }
            },
            {
                "error": {
                    "message": "invalid params, context window exceeds limit (2013) PRIVATE-PROMPT"
                }
            },
            {"base_resp": {"status_code": True}},
            {"base_resp": {"status_code": [2013]}},
            {"base_resp": {"status_code": 999999}},
            {"error": {"code": "2013"}, "code": "1008"},
            {"error": {"code": "2013"}, "base_resp": {"status_code": 1008}},
            {
                "error": {
                    "code": "1008",
                    "message": "invalid params, context window exceeds limit (2013)",
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            client = LLMClient(Config(work_dir=Path(directory), api_key="fake-secret"))
            for body in bodies:
                error = APIStatusError(
                    "PRIVATE-PROMPT",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://example.invalid")
                    ),
                    body=body,
                )
                client._sanitize_transport_error(error)
                self.assertNotIn("provider_error_category", exception_evidence(error), str(body))
                self.assertIsNone(error.body)
            await client.aclose()

    def test_free_form_diagnostic_category_cannot_leak(self):
        error = ValueError("PRIVATE-PROMPT")
        error.transport_diagnostics = {"provider_error_category": "PRIVATE-PROMPT"}
        self.assertNotIn("PRIVATE-PROMPT", str(exception_evidence(error)))


if __name__ == "__main__":
    unittest.main()
