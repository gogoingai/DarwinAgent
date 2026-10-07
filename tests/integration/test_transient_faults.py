"""瞬时服务故障分类回归（2026-10-05 B0 四题事故）。

529 overloaded_error 必须在传输层退避重试（与 429/503 同族）；InternalServerError
等服务瞬时族在题级故障重试里必须可重试；确定性工具错误与协议耗尽保持不可重试。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai

from darwinagent.config import Config
from darwinagent.experiments.recovery import _retryable_answer
from darwinagent.llm.client import LLMClient, TransportExhausted


def _overloaded(code=529):
    return openai.APIStatusError(
        f"Error code: {code}",
        response=httpx.Response(code, request=httpx.Request("POST", "https://gw/v1/chat")),
        body={"error": {"type": "overloaded_error", "message": "当前服务集群负载较高"}},
    )


class _Completions:
    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    async def create(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise _overloaded()

        async def stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))], usage=None
            )
            yield SimpleNamespace(
                choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)
            )

        return stream()


class OverloadRetryTests(unittest.TestCase):
    def test_529_is_retried_at_transport_layer(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(api_key="fake", work_dir=Path(td), max_retries=3)

            async def run():
                async with LLMClient(cfg) as client:
                    completions = _Completions(failures=1)
                    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
                    client._site_for = lambda _model: (fake, asyncio.Semaphore(1))
                    result = await client.chat(
                        role="schema",
                        messages=[{"role": "user", "content": "q"}],
                        namespace="test",
                        use_cache=False,
                    )
                    return result.content, completions.calls

            content, calls = asyncio.run(run())
            self.assertEqual(content, "ok")
            self.assertEqual(calls, 2, "一次 529 后必须退避重试而不是立刻抛出")

    def test_persistent_529_wraps_into_transport_exhausted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(api_key="fake", work_dir=Path(td), max_retries=2)

            async def run():
                async with LLMClient(cfg) as client:
                    completions = _Completions(failures=99)
                    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
                    client._site_for = lambda _model: (fake, asyncio.Semaphore(1))
                    with self.assertRaises(TransportExhausted):
                        await client.chat(
                            role="schema",
                            messages=[{"role": "user", "content": "q"}],
                            namespace="test",
                            use_cache=False,
                        )
                    return completions.calls

            calls = asyncio.run(run())
            self.assertEqual(calls, 2, "耗尽后包装为 TransportExhausted，题级可重试")


class RetryableAnswerTests(unittest.TestCase):
    @staticmethod
    def _answer(error, trace=()):
        return SimpleNamespace(error=error, trace=trace)

    def test_service_transient_family_is_retryable(self):
        for error in (
            "InternalServerError: Error code: 529 - overloaded",
            "APIConnectionError: connection reset",
            "APITimeoutError: timed out",
            "TransportExhausted: answer: last error",
            "EmptyCompletion: Empty generation response",
        ):
            self.assertTrue(_retryable_answer(self._answer(error)), error)

    def test_deterministic_and_protocol_stay_non_retryable(self):
        for error in (
            "SandboxError: Restricted execution budget exhausted",
            "ValueError: tool.params: relation",
            "ProtocolError: Feedback retries exhausted",
        ):
            self.assertFalse(_retryable_answer(self._answer(error)), error)

    def test_tool_error_trace_overrides_transient_final_error(self):
        answer = self._answer(
            "InternalServerError: Error code: 529",
            trace=[{"stage": "tool_error", "asset_id": "f_x"}],
        )
        self.assertFalse(_retryable_answer(answer))


if __name__ == "__main__":
    unittest.main()
