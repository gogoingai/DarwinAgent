"""Offline lifecycle boundaries between Wiki evidence, durable adoption and cleanup."""

import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.kernel import TaskSpec
from darwinagent.runtime.deadline import ROUND_DEADLINE
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK
from tests.support.recorded_fastloop import FastLoopExperiment, _run


class RunnerBoundaryTests(unittest.TestCase):
    def test_wiki_observes_original_decision_publication_order(self):
        for limit in (None, 30):
            with self.subTest(deadline=limit), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = FastLoopExperiment(root, round_deadline_s=limit)
                record = WikiMaintainer.record
                observed = []

                async def observe(
                    maintainer,
                    stage,
                    kind,
                    facts,
                    *,
                    root=root,
                    limit=limit,
                    observed=observed,
                    record=record,
                    **kwargs,
                ):
                    if stage == "R1" and kind in ("formal", "decision"):
                        decision_path = root / "R1/decision.json"
                        self.assertEqual(decision_path.exists(), limit is None)
                        pointer = json.loads((root / "published/current.json").read_text())
                        b0 = json.loads((root / "B0/stage.json").read_text())
                        self.assertEqual(pointer["version"], b0["asset_version"])
                        if decision_path.exists():
                            self.assertTrue(json.loads(decision_path.read_text())["accepted"])
                        observed.append(kind)
                    return await record(maintainer, stage, kind, facts, **kwargs)

                with mock.patch.object(WikiMaintainer, "record", observe):
                    summary = _run(runner)
                self.assertEqual(observed, ["formal", "decision"])
                pointer = json.loads((root / "published/current.json").read_text())
                self.assertEqual(pointer["version"], summary["adopted_version"])
                self.assertTrue(summary["rounds"][0]["accepted"])

    def test_publication_failure_closes_clients_resets_context_and_restores_before_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=30)
            publish = runner.revisions.publish
            closed = []
            persisted = []

            async def close(client):
                closed.append(client)

            def interrupted(bundle, destination, decision):
                if decision.get("candidate_version"):
                    saved = json.loads((root / "R1/decision.json").read_text())
                    self.assertTrue(saved["accepted"])
                    persisted.append(saved)
                    raise OSError("publication interrupted")
                return publish(bundle, destination, decision)

            async def run_with_outer_context():
                outer_deadline = time.monotonic() + 300
                token = ROUND_DEADLINE.set(outer_deadline)
                try:
                    with self.assertRaisesRegex(OSError, "publication interrupted"):
                        await runner.run(
                            runner.case.id,
                            TaskSpec.load(TASK / "task.yaml"),
                            rounds=1,
                            scope=("S", "F", "C", "P"),
                        )
                    self.assertEqual(ROUND_DEADLINE.get(), outer_deadline)
                finally:
                    ROUND_DEADLINE.reset(token)

            with (
                mock.patch.object(LedgerRecordedClient, "aclose", close),
                mock.patch.object(runner.revisions, "publish", interrupted),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                asyncio.run(run_with_outer_context())
            self.assertIsNone(runner._round_deadline)
            self.assertTrue(all(client in closed for client in runner.created))
            pointer = json.loads((root / "published/current.json").read_text())
            self.assertEqual(pointer["version"], persisted[0]["base_version"])
            restored = FastLoopExperiment(root, round_deadline_s=30)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(
                    restored.run(
                        restored.case.id,
                        TaskSpec.load(TASK / "task.yaml"),
                        rounds=0,
                        resume=True,
                        scope=("S", "F", "C", "P"),
                    )
                )
            self.assertEqual(summary["adopted_version"], persisted[0]["candidate_version"])
            self.assertEqual(len(summary["rounds"]), 1)
            self.assertFalse((root / "R2").exists())
            pointer = json.loads((root / "published/current.json").read_text())
            self.assertEqual(pointer["version"], summary["adopted_version"])
