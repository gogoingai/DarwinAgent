"""Declared invalid-reply projections must preserve complete raw trajectory."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, ToolSpec
from darwinagent.runtime.artifacts import atomic_json, digest
from datasets.lift.full_trajectory import IncompleteTrajectory, extract_trajectory
from tests.support.full_lift_trajectory import RecordedWorker, read_tool


class ContextProjectionTrajectoryTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, root):
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        worker = RecordedWorker()
        worker.replies = iter([{"unexpected": "x" * 5000}, {"final_response": "done"}])
        atomic_json(
            root / "session-identity.json",
            {"role": "worker", "task_id": "task", "kernel_id": None, "session": "s"},
        )
        backend = ExecutionBackend(
            ReActAgent(invalid_reply_context_chars=256),
            [ToolSpec("read", read_tool)],
            ExecutionProfile("test", ("read",)),
            worker,
            root / "episodes",
        )
        history = [{"role": "user", "content": "native task"}]
        task = AgentTask("task", "synthetic", parameters={"native_messages": history})
        ep = (await backend.run(task, run_id="turn-0")).to_dict()
        self.assertEqual(ep["status"], "completed")
        turn = {"message_hash": digest("native task"), "status": "completed", "episode": ep}
        atomic_json(root / "turns/0.json", turn)
        atomic_json(
            root / "chat.json",
            {"history": history + [{"role": "assistant", "content": "done"}], "turns": [turn]},
        )

    async def test_invalid_reply_excerpt_is_verified_and_raw_reply_remains_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.fixture(root)
            before = {str(p): p.read_bytes() for p in root.rglob("*.json")}
            transcript = extract_trajectory(root)
            self.assertEqual(len(transcript["raw_requests"]), 2)
            self.assertIn("x" * 5000, transcript["raw_requests"][0]["response"]["content"])
            self.assertTrue(
                any("x" * 5000 in str(m.get("content")) for m in transcript["all_messages"])
            )
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*.json")})

    async def test_undeclared_prefix_rewrite_is_rejected_even_with_valid_step_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.fixture(root)
            step = root / "episodes/turn-0/steps/000001.json"
            row = json.loads(step.read_text())
            assistant = next(m for m in row["input"]["messages"] if m["role"] == "assistant")
            assistant["content"] = "forged working prefix"
            semantic = {
                k: v for k, v in row["input"].items() if k not in {"temperature", "max_tokens"}
            }
            row["fingerprint"] = digest({"kind": "model", "input": semantic})
            atomic_json(step, row)
            with self.assertRaises(IncompleteTrajectory):
                extract_trajectory(root)

    async def test_forged_projection_is_rejected_with_consistent_event_and_result_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.fixture(root)
            episode = root / "episodes/turn-0"
            path = next(
                p
                for p in (episode / "events").glob("*.json")
                if json.loads(p.read_text())["event"]["kind"] == "context_projection"
            )
            saved = json.loads(path.read_text())
            saved["event"]["projected_content"] = "forged projection"
            saved["digest"] = digest(saved["event"])
            atomic_json(path, saved)
            result = json.loads((episode / "result.json").read_text())
            result["result"]["events"][saved["event"]["index"]] = saved["event"]
            result["digest"] = digest(result["result"])
            atomic_json(episode / "result.json", result)
            turn = json.loads((root / "turns/0.json").read_text())
            turn["episode"] = result["result"]
            atomic_json(root / "turns/0.json", turn)
            chat = json.loads((root / "chat.json").read_text())
            chat["turns"][0]["episode"] = result["result"]
            atomic_json(root / "chat.json", chat)
            with self.assertRaises(IncompleteTrajectory):
                extract_trajectory(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
