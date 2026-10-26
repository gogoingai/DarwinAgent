import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent
from tests.support.agent_runtime import RawRecordedClient

try:
    from datasets.lift.agents import NativeReAct
    from datasets.lift.full_agents import FullNativeReAct
except ModuleNotFoundError as exc:
    if exc.name not in {"src", "src.lift"}:
        raise
    NATIVE = False
else:
    NATIVE = True


@unittest.skipUnless(NATIVE, "Optional pinned LIFT upstream is not configured")
class LiftProtocolIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_limit_cannot_replay_saved_terminal_result(self):
        client = RawRecordedClient(["{", '{"final_response":"done"}'])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = AgentTask("same", "Return a result")
            first = ExecutionBackend(
                NativeReAct(max_protocol_failures=1),
                (),
                ExecutionProfile("fixed", ()),
                client,
                root,
            )
            result = await first.run(task, run_id="same")
            self.assertEqual(result.status, "failed")
            changed = ExecutionBackend(
                NativeReAct(max_protocol_failures=None),
                (),
                ExecutionProfile("fixed", ()),
                client,
                root,
            )
            with self.assertRaises(ValueError):
                await changed.run(task, run_id="same")
            self.assertEqual(len(client.requests), 1)

    def test_historical_adapter_positional_caps_keep_their_slots(self):
        adapter = NativeReAct(16, "worker", ReActAgent.system_prompt, None, 64, 16)
        self.assertEqual(adapter.worker_session_cap, 64)
        self.assertEqual(adapter.worker_turn_model_cap, 16)
        self.assertIsNone(adapter.max_protocol_failures)

    def test_full_adapter_positional_caps_keep_their_slots(self):
        adapter = FullNativeReAct(16, "worker", ReActAgent.system_prompt, None, 64, 16)
        self.assertEqual(adapter.worker_session_cap, 64)
        self.assertEqual(adapter.worker_turn_model_cap, 16)
        self.assertEqual(adapter.max_protocol_failures, 8)

    def test_historical_default_identity_omits_new_optional_configuration(self):
        self.assertNotIn("max_protocol_failures", NativeReAct().identity())
        self.assertNotEqual(
            NativeReAct().identity(), NativeReAct(max_protocol_failures=1).identity()
        )
