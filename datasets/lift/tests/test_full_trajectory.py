"""Offline transcripts from actual backend journals, never live model calls."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, ToolSpec
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.steps import UnknownRequest
from datasets.lift.full_trajectory import (
    IncompleteTrajectory,
    _verify,
    build_native_judge_input,
    extract_trajectory,
    score_trajectory,
)
from tests.support.full_lift_trajectory import RecordedShared, RecordedWorker, read_tool


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"

    async def fixture(self, root, *, macro=False):
        from darwinagent.agent import AgentAsset, AgentKernelAssets

        profile = ExecutionProfile("test", ("read",))
        kernel = None
        worker = RecordedWorker()
        if macro:
            kernel = AgentKernelAssets(
                (
                    AgentAsset(
                        "schema",
                        "S",
                        json.dumps(
                            {
                                "nodes": [
                                    {
                                        "id": "Task",
                                        "source": "task",
                                        "where": {},
                                        "fields": {
                                            "instruction": {"path": "instruction", "type": "string"}
                                        },
                                    }
                                ],
                                "relations": [],
                            }
                        ),
                    ),
                    AgentAsset(
                        "twice",
                        "F",
                        'def run(params):\n    first = call("read", {"n": 1})\n    second = call("read", {"n": 2})\n    return {"first":first,"second":second}\n',
                        trial_inputs=({},),
                        schema_dependencies=("schema",),
                    ),
                    AgentAsset(
                        "worker",
                        "P",
                        "$instruction\n$tools\n$view",
                        role="worker",
                        schema_dependencies=("schema",),
                    ),
                ),
                profile,
            ).export(root / "kernel")
            worker.replies = iter(
                [
                    {"tool": "twice", "params": {}},
                    {"final_response": "first"},
                    {"final_response": "corrected"},
                ]
            )
        atomic_json(
            root / "session-identity.json",
            {
                "role": "worker",
                "task_id": "scene/q1",
                "kernel_id": kernel.content_id if kernel else None,
                "session": "s",
            },
        )
        backend = ExecutionBackend(
            ReActAgent(),
            [ToolSpec("read", read_tool)],
            profile,
            worker,
            root / "episodes",
        )
        history, turns = [], []
        for i, prompt in enumerate(["native task", "native correction"]):
            history.append({"role": "user", "content": prompt})
            task = AgentTask("scene/q1", "synthetic", parameters={"native_messages": history})
            ep = (await backend.run(task, kernel, run_id=f"turn-{i}")).to_dict()
            receipt = {"message_hash": digest(prompt), "status": ep["status"], "episode": ep}
            atomic_json(root / "turns" / f"{i}.json", receipt)
            turns.append(receipt)
            history.append({"role": "assistant", "content": ep["final_response"]})
        atomic_json(root / "chat.json", {"history": history, "turns": turns})

    async def test_real_journal_order_failure_correction_and_raw_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.fixture(root)
            transcript = extract_trajectory(root)
            messages = transcript["all_messages"]
            self.assertEqual(transcript["leaf_calls"], 2)
            self.assertEqual(len(transcript["raw_requests"]), 3)
            tool_positions = [i for i, m in enumerate(messages) if m["role"] == "tool"]
            self.assertEqual(len(tool_positions), 2)
            self.assertIn("failure", messages[tool_positions[1]]["content"])
            self.assertLess(
                tool_positions[1],
                next(i for i, m in enumerate(messages) if m.get("content") == "first"),
            )
            self.assertLess(
                next(i for i, m in enumerate(messages) if m.get("content") == "first"),
                next(i for i, m in enumerate(messages) if m.get("content") == "native correction"),
            )
            self.assertEqual(messages[-1]["content"], "corrected")
            _verify(transcript)
            step = root / "episodes/turn-0/steps/000001.json"
            step.unlink()
            with self.assertRaises(IncompleteTrajectory):
                extract_trajectory(root)
            with self.assertRaises((ValueError, FileNotFoundError)):
                _verify(transcript)

    async def test_expanded_macro_failure_and_native_prompt_exact(self):
        import hashlib

        import pandas as pd
        from src.postprocess import judge

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.fixture(root, macro=True)
            transcript = extract_trajectory(root)
            self.assertEqual(transcript["leaf_calls"], 2)
            source = str(Path(judge.__file__).resolve())
            envelope = build_native_judge_input(
                transcript,
                task_query="synthetic",
                trajectory_reqs="synthetic requirement",
                source_hashes={source: hashlib.sha256(Path(source).read_bytes()).hexdigest()},
            )
            expected = judge.build_judge_messages(
                pd.Series(
                    {
                        "task_query": "synthetic",
                        "trajectory_reqs": "synthetic requirement",
                        "all_messages": transcript["all_messages"],
                    }
                )
            )
            self.assertEqual(envelope["messages"], expected)
            formatted = envelope["messages"][1]["content"]
            self.assertIn('"name": "read"', formatted)
            self.assertIn("read failed (ValueError)", formatted)
            self.assertIn("native correction", formatted)
            self.assertEqual(judge.extract_score_from_response('{"trajectory_score": 0.75}'), 0.75)
            with self.assertRaises(ValueError):
                build_native_judge_input(
                    transcript, task_query="synthetic", trajectory_reqs="", source_hashes={}
                )

    async def test_missing_turn_unknown_and_new_step_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.fixture(root)
            transcript = extract_trajectory(root)
            path = root / "episodes/turn-0/steps/000004.json"
            atomic_json(path, {"state": "submitted"})
            with self.assertRaises(ValueError):
                _verify(transcript)
            with self.assertRaises(IncompleteTrajectory):
                extract_trajectory(root)


class JudgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"

    def envelope(self):
        transcript = {"source_hashes": {}}
        transcript["sha256"] = digest(transcript)
        value = {
            "source_hashes": {},
            "transcript": transcript,
            "task_id": "scene/q1",
            "kernel_id": "K",
            "messages": [{"role": "user", "content": "synthetic"}],
        }
        return dict(value, sha256=digest(value))

    async def test_completed_replay_and_changed_input_refuses(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("src.postprocess.judge.extract_score_from_response", return_value=0.75),
        ):
            shared, envelope = RecordedShared(), self.envelope()
            first = await score_trajectory(shared, "session", tmp, envelope)
            self.assertEqual(first["score"], 0.75)
            self.assertEqual(await score_trajectory(shared, "session", tmp, envelope), first)
            self.assertEqual(shared.calls, 1)
            envelope["messages"][0]["content"] = "changed"
            envelope["sha256"] = digest({k: v for k, v in envelope.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                await score_trajectory(shared, "session", tmp, envelope)

    async def test_unknown_pause_preserved_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared, envelope = (
                RecordedShared(error=TimeoutError("private payload")),
                self.envelope(),
            )
            with self.assertRaises(TimeoutError):
                await score_trajectory(shared, "session", tmp, envelope)
            with self.assertRaises(UnknownRequest):
                await score_trajectory(shared, "session", tmp, envelope)
            self.assertEqual(shared.calls, 1)
            result = json.loads((Path(tmp) / "result.json").read_text())
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("private payload", json.dumps(result))

    async def test_cancelled_known_completion_replays_saved_result(self):
        import asyncio
        from types import SimpleNamespace

        cancelled = asyncio.CancelledError()
        cancelled.completed_result = SimpleNamespace(content='{"trajectory_score":0.75}', usage={})
        with tempfile.TemporaryDirectory() as tmp:
            shared = RecordedShared(error=cancelled)
            with self.assertRaises(asyncio.CancelledError):
                await score_trajectory(shared, "s", tmp, self.envelope())
            result = await score_trajectory(shared, "s", tmp, self.envelope())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0.75)
            self.assertEqual(shared.calls, 1)

    async def test_native_parse_failure_retains_raw_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = RecordedShared(content="unparseable synthetic output")
            result = await score_trajectory(shared, "session", tmp, self.envelope())
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(result["raw_response"], shared.content)
            await score_trajectory(shared, "session", tmp, self.envelope())
            self.assertEqual(shared.calls, 1)


class LedgerJudgeTests(unittest.IsolatedAsyncioTestCase):
    setUp = JudgeTests.setUp
    envelope = JudgeTests.envelope

    async def exercise(self, mode):
        import asyncio
        from functools import partial

        import httpx

        from darwinagent.config import Config
        from darwinagent.llm.client import LLMClient
        from datasets.lift.transport import SharedTransport
        from tests.support.full_lift_trajectory import http_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sends = []
            original_http_client = httpx.AsyncClient

            class MockedHttpClient(original_http_client):
                def __init__(self, **kwargs):
                    super().__init__(
                        transport=httpx.MockTransport(partial(http_handler, mode, sends)), **kwargs
                    )

            cfg = Config(
                api_base_url="https://synthetic.invalid/v1",
                api_key="synthetic-offline",
                model_strong="synthetic",
                model_middle="synthetic",
                model_fast="synthetic",
                model_profiles=False,
                max_retries=1,
                max_http_requests=1,
                request_budget_path=root / "ledger.json",
                work_dir=root / "client",
            )
            cfg.role_tiers["trajectory_judge"] = "strong"
            with patch("darwinagent.llm.client.httpx.AsyncClient", new=MockedHttpClient):
                client = LLMClient(cfg)
            shared = SharedTransport(client, root / "shared")
            try:
                if mode in {"unknown", "cancel"}:
                    with self.assertRaises((Exception, asyncio.CancelledError)):
                        await score_trajectory(shared, "s", root / "judge", self.envelope())
                    with self.assertRaises(UnknownRequest):
                        await score_trajectory(shared, "s", root / "judge", self.envelope())
                    with self.assertRaises(Exception):
                        shared.check()
                else:
                    result = await score_trajectory(shared, "s", root / "judge", self.envelope())
                    self.assertEqual(
                        result["status"], "completed" if mode == "completed" else "failed"
                    )
                    self.assertEqual(
                        await score_trajectory(shared, "s", root / "judge", self.envelope()), result
                    )
                self.assertEqual(client.http_attempts(), 1)
                self.assertEqual(len(sends), 1)
                receipts = [
                    json.loads(p.read_text()) for p in (shared.root / "requests").glob("*.json")
                ]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0]["role"], "trajectory_judge")
                self.assertEqual(receipts[0]["actual_http_attempts"], 1)
                self.assertEqual(
                    receipts[0]["status"], "unknown" if mode in {"unknown", "cancel"} else mode
                )
                self.assertFalse((shared.root / "worker-calls.json").exists())
                if mode == "completed":
                    exhausted = await score_trajectory(
                        shared, "next", root / "exhausted", self.envelope()
                    )
                    self.assertEqual(exhausted["status"], "budget_exhausted")
                    self.assertEqual(client.http_attempts(), 1)
                    self.assertEqual(len(sends), 1)
            finally:
                await client.aclose()

    async def test_real_shared_http_ledger_completed_replay_and_budget(self):
        await self.exercise("completed")

    async def test_real_shared_known_provider_failure(self):
        await self.exercise("failed")

    async def test_real_shared_unknown_gate(self):
        await self.exercise("unknown")

    async def test_real_shared_cancellation_gate(self):
        await self.exercise("cancel")
