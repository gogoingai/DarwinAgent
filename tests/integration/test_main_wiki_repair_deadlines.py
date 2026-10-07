"""Offline regression scenarios for deadlines."""

import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.experiments.proposal import ProposalGenerator
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.kernel import TaskSpec
from darwinagent.runtime.deadline import ROUND_DEADLINE, RoundDeadlineExceeded
from darwinagent.vector.embedder import Embedder
from tests.support.device import TASK
from tests.support.recorded_fastloop import FastLoopExperiment, _run


class FullRoundDeadlineTests(unittest.TestCase):
    def verify_timeout(self, root, summary):
        decision = summary["rounds"][0]
        self.assertEqual(decision["status"], "round_timeout")
        self.assertFalse(decision["accepted"])
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["completed_rounds"], 0)
        pointer = json.loads((root / "published/current.json").read_text())
        self.assertEqual(pointer["version"], decision["base_version"])
        self.assertTrue((root / "R1/timeout.json").exists())

    def test_inflight_proposal_cancelled_before_formal_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = ProposalGenerator.propose
            cancelled = []

            async def slow(self, *args, **kwargs):
                try:
                    await asyncio.sleep(0.6)
                    return await original(self, *args, **kwargs)
                finally:
                    cancelled.append(True)

            with mock.patch.object(ProposalGenerator, "propose", slow):
                summary = _run(FastLoopExperiment(root, round_deadline_s=0.3), rounds=1)
            self.verify_timeout(root, summary)
            self.assertTrue(cancelled)
            self.assertFalse((root / "R1/stage.json").exists())

    def test_formal_score_overrun_does_not_publish(self):
        class SlowFormal(FastLoopExperiment):
            async def _stage(self, name, *args, **kwargs):
                if name == "R1":
                    await asyncio.sleep(0.6)
                return await super()._stage(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = _run(SlowFormal(root, round_deadline_s=0.3), rounds=1)
            self.verify_timeout(root, summary)
            self.assertFalse((root / "R1/stage.json").exists())

    def test_wiki_overrun_does_not_publish_completed_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=30)
            original = WikiMaintainer.record
            reached_wiki = []

            async def expire_at_wiki(maintainer, stage, kind, *args, **kwargs):
                if stage == "R1" and kind == "decision" and kwargs.get("infer"):
                    scored = json.loads((root / "R1/stage.json").read_text())
                    self.assertEqual(scored["status"], "complete")
                    self.assertEqual(scored["scores"]["completed"], scored["scores"]["total"])
                    reached_wiki.append(stage)
                    # Inject expiration only after formal scoring. Timer cancellation
                    # is exercised separately by the proposal/formal-score probes.
                    runner._round_deadline = time.monotonic() - 1
                    raise RoundDeadlineExceeded("Injected Wiki-phase deadline exceeded")
                return await original(maintainer, stage, kind, *args, **kwargs)

            with mock.patch.object(WikiMaintainer, "record", expire_at_wiki):
                summary = _run(runner, rounds=1)
            self.assertEqual(reached_wiki, ["R1"])
            self.verify_timeout(root, summary)
            self.assertTrue((root / "R1/stage.json").exists())

    def test_resume_keeps_consumed_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=0.001)
            _run(runner, rounds=1)
            budget_path = root / "R1/round-budget.json"
            budget = json.loads(budget_path.read_text())
            # Simulate interruption after budget exhaustion but before terminal decision.
            (root / "R1/decision.json").unlink()
            resumed = FastLoopExperiment(root, round_deadline_s=0.001)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(
                    resumed.run(
                        resumed.case.id,
                        TaskSpec.load(TASK / "task.yaml"),
                        rounds=1,
                        resume=True,
                        scope=("S", "F", "C", "P"),
                    )
                )
            self.verify_timeout(root, summary)
            self.assertEqual(json.loads(budget_path.read_text()), budget)

    def test_native_embedding_uses_remaining_budget_without_retries(self):
        calls = []

        def post(*args, **kwargs):
            calls.append(kwargs["timeout"])
            time.sleep(0.025)
            return mock.Mock(status_code=200, json=lambda: {"data": [{"embedding": [1.0]}]})

        token = ROUND_DEADLINE.set(time.monotonic() + 0.015)
        try:
            with (
                mock.patch("darwinagent.vector.embedder.httpx.post", post),
                mock.patch("darwinagent.vector.embedder.time.sleep", wraps=time.sleep),
            ):
                with self.assertRaises(RoundDeadlineExceeded):
                    Embedder("https://unused.example", "unused", "recorded").embed("probe")
            self.assertEqual(len(calls), 1)
            self.assertLessEqual(calls[0], 0.015)
        finally:
            ROUND_DEADLINE.reset(token)
