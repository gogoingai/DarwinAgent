"""Optional generation declaration preserves the default and rejects changed resume."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent
from tests.support.agent_runtime import RecordedClient


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default4096_explicit16384_and_changed_identity_resume_rejects(self):
        class Client(RecordedClient):
            def __init__(self):
                super().__init__([{"final_response": "done"}])
                self.requests = []

            async def chat(self, **kwargs):
                self.requests.append(kwargs)
                return await super().chat(**kwargs)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = AgentTask("q", "deliver")
            first = Client()
            default = ExecutionBackend(
                ReActAgent(), [], ExecutionProfile("generation", ()), first, root / "default"
            )
            self.assertEqual((await default.run(task, run_id="one")).status, "completed")
            self.assertEqual(first.requests[0]["max_tokens"], 4096)
            second = Client()
            explicit = ExecutionBackend(
                ReActAgent(max_tokens=16384), [], default.profile, second, root / "explicit"
            )
            result = await explicit.run(task, run_id="one")
            self.assertEqual(second.requests[0]["max_tokens"], 16384)
            self.assertEqual((await explicit.run(task, run_id="one")).to_dict(), result.to_dict())
            self.assertEqual(len(second.requests), 1)
            changed = ExecutionBackend(
                ReActAgent(max_tokens=4096), [], default.profile, second, root / "explicit"
            )
            self.assertNotEqual(changed.identity(), explicit.identity())
            with self.assertRaises(ValueError):
                await changed.run(task, run_id="one")
            self.assertEqual(len(second.requests), 1)
        for value in (True, 0, -1, "16384"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReActAgent(max_tokens=value)
