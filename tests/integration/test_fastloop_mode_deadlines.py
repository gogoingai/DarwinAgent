"""Offline regression scenarios for deadlines."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.experiments.proposal import ProposalGenerator
from tests.support.recorded_fastloop import FastLoopExperiment, _run


class RoundBudgetTests(unittest.TestCase):
    def test_round_deadline_persists_and_is_not_formal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=0.001)
            original = ProposalGenerator.propose

            async def fail_first(self, *args, **kwargs):
                if "attempt-0" in str(args[5]):
                    raise ValueError("static candidate shape invalid")
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, "propose", fail_first):
                summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            self.assertFalse(decision["accepted"])
            self.assertEqual(decision["status"], "round_timeout")
            self.assertTrue(
                any("Round deadline" in r for r in decision["reasons"]), decision["reasons"]
            )
            self.assertTrue((root / "R1/timeout.json").exists())
            self.assertFalse((root / "R1/stage.json").exists())
            self.assertEqual(summary["completed_rounds"], 0)

    def test_proposal_attempts_cap_exhausts_before_fifty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, proposal_attempts=1)
            original = ProposalGenerator.propose

            async def fail_first(self, *args, **kwargs):
                if "attempt-0" in str(args[5]):
                    raise ValueError("static candidate shape invalid")
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, "propose", fail_first):
                summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            self.assertEqual(decision["status"], "validation_failed")
            self.assertTrue(any("WikiAdmissionExhausted" in r for r in decision["reasons"]))
            first = json.loads((root / "R1/optimization/attempt-0/status.json").read_text())
            self.assertEqual(first["state"], "failed")
            self.assertFalse((root / "R1/optimization/attempt-1").exists())
