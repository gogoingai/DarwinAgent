import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.kernel import TaskSpec
from tests.support.device import TASK
from tests.support.recorded_experiment import (
    RecordedExperiment,
)


class FullExperimentControl(unittest.TestCase):
    def execute(self, stale=False):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        runner = RecordedExperiment(root, stale)
        spec = TaskSpec.load(TASK / "task.yaml")
        with contextlib.redirect_stdout(io.StringIO()):
            summary = asyncio.run(runner.run(runner.case.id, spec))
        return root, runner, summary

    def test_full_bootstrap_proposal_score_adopt_then_reject_tie(self):
        root, runner, summary = self.execute()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual([d["accepted"] for d in summary["rounds"]], [True, False])
        self.assertIn("primary_not_strictly_improved", summary["rounds"][1]["reasons"])
        published = json.loads((root / "published/current.json").read_text())
        self.assertEqual(published["version"], summary["rounds"][0]["candidate_version"])
        for stage in ["B0", "R1", "R2"]:
            result = json.loads(
                (root / stage / "generation" / runner.case.id / "result.json").read_text()
            )
            self.assertEqual(result["answers"][0]["status"], "answered")
            self.assertTrue((root / stage / "evaluation" / f"{runner.case.id}.json").exists())
        second = json.loads((root / "R2/proposal-call.json").read_text())
        self.assertEqual(second["input"]["base_version"], published["version"])
        self.assertEqual(
            second["input"]["task_training_feedback"]["scores"]["metrics"]["precise"], 1
        )

    def test_stale_candidate_rejected_without_generation_or_publication(self):
        root, runner, summary = self.execute(stale=True)
        self.assertEqual(summary["status"], "failed")
        rejected = summary["rounds"][1]
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["status"], "validation_failed")
        self.assertFalse((root / "R2/generation").exists())
        published = json.loads((root / "published/current.json").read_text())
        self.assertEqual(published["version"], summary["rounds"][0]["candidate_version"])
