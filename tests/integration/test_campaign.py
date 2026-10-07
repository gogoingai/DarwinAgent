import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.kernel import TaskSpec
from tests.support.device import TASK
from tests.support.recorded_campaign import (
    RecordedCampaign,
    execute,
    protocol,
)

"""Full three-set campaign state machine on recorded transport."""


class ThreeSetCampaign(unittest.TestCase):
    def test_adopt_validate_select_test_and_ledger(self):
        root, td, summary = execute(protocol(rounds=1))
        self.addCleanup(td.cleanup)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual([d["accepted"] for d in summary["train"]["rounds"]], [True])
        b0 = summary["candidates"][0]["version"]
        self.assertEqual(len(summary["candidates"]), 2)
        self.assertNotEqual(summary["selected"], b0)
        self.assertEqual(len(summary["test"]), 2)
        self.assertEqual(summary["question_runs"], 6)  # 2 train + 2 validation + 2 test
        selected = json.loads((root / "selected.json").read_text())
        self.assertTrue(selected["sealed_before_test"])
        self.assertEqual(selected["selected"], summary["selected"])

    def test_operator_stop_locks_b0_and_single_test_run(self):
        root, td, summary = execute(protocol(rounds=None), stop=True)
        self.addCleanup(td.cleanup)
        self.assertEqual(summary["status"], "complete")
        self.assertTrue(summary["operator_stopped"])
        self.assertEqual(len(summary["candidates"]), 1)  # B0 only
        self.assertIsNone(summary["selection"]["selected"])  # nothing beats B0
        self.assertEqual(summary["selected"], summary["candidates"][0]["version"])
        self.assertEqual(len(summary["test"]), 1)  # identical fingerprints run once
        self.assertEqual(summary["question_runs"], 3)  # 1 train + 1 validation + 1 test

    def test_safety_cap_blocks_before_overshoot(self):
        with self.assertRaises(ValueError):
            execute(protocol(rounds=1, cap=5))

    def test_resume_after_completion_is_sealed(self):
        root, td, summary = execute(protocol(rounds=1))
        self.addCleanup(td.cleanup)
        again = json.loads((root / "campaign-summary.json").read_text())
        with self.assertRaises(ValueError) as caught:
            execute(protocol(rounds=1), resume=True, root=root)
        self.assertIn("sealed", str(caught.exception))
        # The sealed summary on disk is untouched by the refused resume.
        self.assertEqual(json.loads((root / "campaign-summary.json").read_text()), again)

    def test_multi_case_splits_aggregate_and_ledger(self):
        root, td, summary = execute(protocol(rounds=1, multi=True))
        self.addCleanup(td.cleanup)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(len(summary["candidates"]), 2)
        self.assertNotEqual(summary["selected"], summary["candidates"][0]["version"])
        # 每阶段 2 case × 1 题：训练 2 阶段 + 验证 2 候选 + 测试 2 版本 = 12 题次
        self.assertEqual(summary["question_runs"], 12)
        # 验证聚合：B0 两 case 各 0 分、候选各 1 分 → 聚合 0 vs 2
        b0 = summary["candidates"][0]["version"]
        self.assertEqual(summary["validation"][b0]["metrics"], {"precise": 0, "lenient": 0})
        cand = summary["selected"]
        self.assertEqual(summary["validation"][cand]["metrics"], {"precise": 2, "lenient": 2})
        self.assertEqual(summary["validation"][b0]["total"], 2)

    def test_precheck_wrong_identity_refused(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "precheck.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "checks": {},
                    "identity": {"transport": {}, "config": "deadbeef", "framework": "deadbeef"},
                }
            )
        )
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        from darwinagent.kernel import TaskSpec

        with self.assertRaises(ValueError) as caught:
            asyncio.run(controller.run(TaskSpec.load(TASK / "task.yaml")))
        self.assertIn("identity mismatch", str(caught.exception))

    def test_missing_precheck_refuses_to_start(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1), frozen_files=())
        with self.assertRaises(ValueError):
            asyncio.run(controller.run(TaskSpec.load(TASK / "task.yaml")))


if __name__ == "__main__":
    unittest.main()
