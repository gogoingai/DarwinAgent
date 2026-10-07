"""Recorded stability regressions for model transport."""

import asyncio
import unittest
from types import SimpleNamespace

from darwinagent.agents.protocol import ModelSession
from darwinagent.config import RunConfig
from tests.support.preflight import preflight_sync


class ModelTransportTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_model_session_does_not_reclassify_client_value_error(self):
        class Client:
            calls = 0

            async def chat(self, **_kwargs):
                self.calls += 1
                raise ValueError("client configuration invalid")

        client = Client()
        session = ModelSession(client, RunConfig(protocol_attempts=2), "test")
        with self.assertRaisesRegex(ValueError, "client configuration invalid"):
            asyncio.run(session.request("answer", "system", {}, lambda value: value))
        self.assertEqual(client.calls, 1)
        self.assertEqual(session.events[0]["status"], "transport_or_budget_error")

    def test_model_session_corrects_invalid_model_parameters(self):
        class Client:
            calls = 0

            async def chat(self, **_kwargs):
                self.calls += 1
                return SimpleNamespace(content=f'{{"count":{self.calls}}}')

        def validator(value):
            if value["count"] != 2:
                raise ValueError("Invalid model parameter")
            return value

        client = Client()
        session = ModelSession(client, RunConfig(protocol_attempts=2), "test")
        self.assertEqual(
            asyncio.run(session.request("answer", "system", {}, validator)), {"count": 2}
        )
        self.assertEqual([event["status"] for event in session.events], ["protocol_error", "ok"])
