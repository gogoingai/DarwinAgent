"""Explicit provider controls survive generic OpenAI-compatible transport."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import Config
from darwinagent.llm.client import LLMClient


class GenericThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_adaptive_uses_supplied_endpoint_and_model(self):
        captured = {}

        async def stream():
            yield SimpleNamespace(
                model="supplied-model",
                id="reply",
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content="{}"))],
            )

        async def create(**kwargs):
            captured.update(kwargs)
            return stream()

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                api_base_url="https://supplied.example/v1",
                api_key="test-secret",
                model_strong="supplied-model",
                reasoning_effort="low",
                thinking_type="adaptive",
                work_dir=Path(tmp),
                max_retries=1,
            )
            async with LLMClient(cfg) as client:
                sdk, _ = client._site_for(cfg.model_strong)
                sdk.chat.completions.create = create
                reply = await client.chat(
                    role="bootstrap", messages=[], max_tokens=500, use_cache=False
                )
            self.assertEqual(reply.content, "{}")
            self.assertEqual(captured["model"], "supplied-model")
            self.assertEqual(
                captured["extra_body"],
                {"reasoning_effort": "low", "thinking": {"type": "adaptive"}},
            )
            self.assertEqual(captured["max_tokens"], 500)
