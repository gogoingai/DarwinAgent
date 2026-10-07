"""Offline regression scenarios for protocol identity."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import Config as _Cfg
from darwinagent.config import RunConfig as _RC
from darwinagent.contracts import (
    EvaluationResult,
)
from darwinagent.experiments.spec import precheck_identity
from tests.support.anchored import anchored_bundle
from tests.support.evaluators import FaultyStageEvaluator


class RevisionProtocolIdentityTests(unittest.TestCase):
    """P1 train-stage faults reach the totals; P2 the report persists before sealing and
    rebuilds from artifacts; P2 an oversized scores skeleton refuses the proposal; P2 the
    revision protocol matches the bundle's real graph mode."""

    def test_train_stage_faults_surface_in_run_status(self):
        import contextlib
        import io

        from darwinagent.kernel import TaskSpec
        from tests.support.device import TASK
        from tests.support.recorded_experiment import RecordedExperiment

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        runner = RecordedExperiment(
            root, evaluator=lambda transport, path: FaultyStageEvaluator(path)
        )
        with contextlib.redirect_stdout(io.StringIO()):
            summary = asyncio.run(
                runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=2)
            )
        # R1 因故障无法完成评分：候选被拒是正常结果，但故障必须进入总状态
        self.assertEqual(summary["unhealthy_stages"]["R1"]["evaluation_faults"], 1)
        self.assertEqual(summary["unhealthy_stages"]["R1"]["completed"], 0)
        self.assertEqual(summary["status"], "failed")

    def test_train_stages_surface_in_campaign_statuses(self):
        from tests.support.recorded_campaign import RecordedCampaign, protocol

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        train = root / "train"
        (train / "B0").mkdir(parents=True)
        (train / "B0" / "stage.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "cases": ["train-case"],
                    "asset_version": "b0",
                    "scores": {
                        "completed": 1,
                        "total": 1,
                        "generation_faults": 0,
                        "evaluation_faults": 0,
                    },
                }
            )
        )
        (train / "R1").mkdir(parents=True)
        (train / "R1" / "stage.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "cases": ["train-case"],
                    "asset_version": "r1",
                    "scores": {
                        "completed": 0,
                        "total": 1,
                        "generation_faults": 1,
                        "evaluation_faults": 1,
                    },
                }
            )
        )
        statuses = controller._stage_statuses()
        self.assertEqual(statuses["train/B0"]["status"], "complete")
        unhealthy = {k: v for k, v in statuses.items() if v["status"] != "complete"}
        self.assertIn("train/R1", unhealthy)
        self.assertEqual(unhealthy["train/R1"]["case"], "train-case")

    def test_report_persisted_before_seal_and_rebuilds_from_artifacts(self):
        import contextlib
        import io
        from unittest import mock

        import darwinagent.experiments.campaign as camp
        from darwinagent.kernel import TaskSpec
        from tests.support.device import TASK
        from tests.support.recorded_campaign import RecordedCampaign, protocol

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        task = TaskSpec.load(TASK / "task.yaml")
        (root / "precheck.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "checks": {},
                    "identity": precheck_identity(_Cfg(), _RC(protocol_attempts=1)),
                }
            )
        )
        real_atomic = camp.atomic_json

        def flaky(path, data):
            if Path(path).name == "campaign-summary.json":
                raise OSError("disk full")
            return real_atomic(path, data)

        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(camp, "atomic_json", side_effect=flaky):
                with self.assertRaises(OSError):
                    asyncio.run(controller.run(task))
        # 报告未落盘时绝不封存：phase 停在 test，可恢复
        self.assertEqual(json.loads((root / "campaign.json").read_text())["phase"], "test")
        self.assertFalse((root / "campaign-summary.json").exists())
        controller2 = RecordedCampaign(root, spec=protocol(rounds=1))
        with contextlib.redirect_stdout(io.StringIO()):
            summary2 = asyncio.run(controller2.run(task, resume=True))
        self.assertEqual(json.loads((root / "campaign.json").read_text())["phase"], "done")
        # 已封存但报告丢失：只从产物重建报告，不重新执行（台账不变、内容一致）
        (root / "campaign-summary.json").unlink()
        ledger_before = (root / "question_runs.jsonl").read_text()
        controller3 = RecordedCampaign(root, spec=protocol(rounds=1))
        with contextlib.redirect_stdout(io.StringIO()):
            summary3 = asyncio.run(controller3.run(task, resume=True))
        self.assertEqual(summary3, summary2)
        self.assertEqual((root / "question_runs.jsonl").read_text(), ledger_before)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(controller3.run(task, resume=True))
        self.assertIn("sealed", str(caught.exception))

    def test_oversize_scores_skeleton_refuses_feedback(self):
        from darwinagent.contracts import RunResult
        from darwinagent.experiments.feedback import training_feedback

        class C:
            id = "c"

        huge = EvaluationResult({f"m{i}": 0 for i in range(4000)}, 0, 0, 0, 0, ())
        with self.assertRaises(ValueError) as caught:
            training_feedback([C()], [RunResult("c", "id", "v", (), 0)], [("c", ())], huge)
        self.assertIn("预算", str(caught.exception))

    def anchored_bundle(self, root):
        return anchored_bundle(root / "anchored")

    def test_revision_protocol_matches_bundle_mode(self):
        from darwinagent.experiments.bootstrap import revision_protocol
        from tests.support.device import spec

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        legacy = revision_protocol(spec(root / "assets").bundle)
        self.assertNotIn("fact-anchored", legacy)
        self.assertNotIn("AtomicFact", legacy)
        self.assertIn('"patches"', legacy)
        self.assertNotIn('{"assets"', legacy)
        self.assertIn("S may be revised", legacy)
        anchored = revision_protocol(self.anchored_bundle(root))
        self.assertIn("fact-anchored", anchored)
        self.assertIn("EXTEND", anchored)
        self.assertIn('"patches"', anchored)
        self.assertNotIn('{"assets"', anchored)
        self.assertNotIn("do not generate, extend or patch it", anchored)  # 矛盾指令已消除

    def test_bootstrap_protocols_carry_single_output_format(self):
        from darwinagent.experiments.bootstrap import ASSET_PROTOCOL, LEGACY_ASSET_PROTOCOL

        for proto in (ASSET_PROTOCOL, LEGACY_ASSET_PROTOCOL):
            self.assertIn('shaped {"assets":[...]}', proto)
            self.assertNotIn("patches", proto)

    def test_proposal_call_records_mode_consistent_protocol(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        self.run_rounds_proxy(root)
        recorded = json.loads((root / "R1" / "proposal-call.json").read_text())
        self.assertIn("protocol", recorded)
        # device 任务（legacy 图模式）的修订协议不得出现原子图指令
        self.assertNotIn("AtomicFact", recorded["protocol"])
        self.assertNotIn("fact-anchored", recorded["protocol"])

    def run_rounds_proxy(self, root):
        import contextlib
        import io

        from darwinagent.kernel import TaskSpec
        from tests.support.device import TASK
        from tests.support.recorded_experiment import RecordedExperiment

        runner = RecordedExperiment(root)
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=1))
