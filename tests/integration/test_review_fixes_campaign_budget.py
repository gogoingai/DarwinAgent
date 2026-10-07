"""Offline regression scenarios for campaign budget."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import Config as _Cfg
from darwinagent.config import RunConfig as _RC
from darwinagent.experiments.spec import precheck_identity


class BudgetReserveBeforeExecution(unittest.TestCase):
    def test_cap_refuses_next_round_before_it_runs(self):
        from tests.support.recorded_campaign import RecordedCampaign, protocol

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "precheck.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "checks": {},
                    "identity": precheck_identity(_Cfg(), _RC(protocol_attempts=1)),
                }
            )
        )
        controller = RecordedCampaign(root, spec=protocol(rounds=1, cap=1))
        from darwinagent.kernel import TaskSpec

        with self.assertRaises(ValueError) as caught:
            asyncio.run(
                controller.run(
                    TaskSpec.load(
                        Path(__file__).resolve().parents[2] / "tasks/device_maintenance/task.yaml"
                    )
                )
            )
        self.assertIn("cap exceeded", str(caught.exception))
        # R1 never generated: the refusal happened before execution, B0 is settled at 1.
        self.assertFalse((root / "train" / "R1" / "generation").exists())
        ledger = (root / "question_runs.jsonl").read_text()
        self.assertIn("train/B0", ledger)
        self.assertNotIn("train/R1", ledger)

    def test_reserve_is_idempotent_and_settle_does_not_recharge(self):
        from tests.support.recorded_campaign import RecordedCampaign, protocol

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        controller._reserve("validation/v1", "val-case", 190)
        controller._reserve("validation/v1", "val-case", 190)  # cache resume
        controller._settle("validation/v1", 190)
        controller._reserve("validation/v1", "val-case", 190)  # resume after settle
        self.assertEqual(controller._ledger_total(), 190)
        self.assertEqual(controller._ledger_rows()["validation/v1"]["state"], "settled")

    def test_decision_scan_skips_resultless_rounds(self):
        from tests.support.recorded_campaign import RecordedCampaign, protocol

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        train = root / "train"
        (train / "B0" / "generation" / "train-case").mkdir(parents=True)
        (train / "B0" / "generation" / "train-case" / "result.json").write_text(
            json.dumps({"answers": [{}], "asset_version": "b0"})
        )
        (train / "R1").mkdir(parents=True)  # admission failed: decision but no generation
        (train / "R1" / "decision.json").write_text(
            json.dumps({"accepted": False, "status": "validation_failed"})
        )
        (train / "R2" / "generation" / "train-case").mkdir(parents=True)
        (train / "R2" / "generation" / "train-case" / "result.json").write_text(
            json.dumps({"answers": [{}], "asset_version": "r2"})
        )
        (train / "R2" / "decision.json").write_text(
            json.dumps({"accepted": True, "candidate_version": "r2"})
        )
        controller._settle_train_from_decisions()
        rows = controller._ledger_rows()
        self.assertIn("train/B0", rows)
        self.assertIn("train/R2", rows)
        self.assertNotIn("train/R1", rows)
