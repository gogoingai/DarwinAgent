"""Offline regression scenarios for publication."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.contracts import (
    EvaluationResult,
    SourceRef,
)
from darwinagent.kernel.revision import (
    AssetPatch,
    AssetRevisionService,
    training_id,
)


class PublicationAndScoredRoundTests(unittest.TestCase):
    """P1 resume keeps adopted rounds; P2 one budget for the whole payload; P3 composite
    question identity through admission; P4 per-case stage aggregation."""

    def test_resume_with_stop_restores_adopted_round_and_pointer(self):
        import contextlib
        import io

        from tests.support.recorded_experiment import RecordedExperiment

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        runner = RecordedExperiment(root)
        from darwinagent.kernel import TaskSpec

        task = TaskSpec.load(
            Path(__file__).resolve().parents[2] / "tasks/device_maintenance/task.yaml"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.run(runner.run(runner.case.id, task, rounds=1))
        self.assertEqual([d["accepted"] for d in first["rounds"]], [True])
        adopted_v = first["adopted_version"]
        (root / "STOP").write_text("operator stop\n")
        with contextlib.redirect_stdout(io.StringIO()):
            again = asyncio.run(
                runner.run(runner.case.id, task, rounds=1, resume=True, stop_file=root / "STOP")
            )
        # 恢复先还原完整决策史：R1 仍在、指针仍指 R1、无新提案
        self.assertEqual([d["accepted"] for d in again["rounds"]], [True])
        self.assertEqual(again["adopted_version"], adopted_v)
        self.assertTrue(again["stopped_by_operator"])
        pointer = json.loads((root / "published" / "current.json").read_text())
        self.assertEqual(pointer["version"], adopted_v)
        self.assertFalse((root / "R2").exists())

    def test_feedback_budget_covers_entire_payload(self):
        from darwinagent.contracts import AnswerResult, RunResult
        from darwinagent.experiments.feedback import FEEDBACK_BUDGET_CHARS, training_feedback

        class C:
            id = "c"

        _ev = (SourceRef("t", "c", "1"),)
        huge_rows = tuple({"i": i, "payload": "x" * 2000} for i in range(40))
        failures_mass = [
            AnswerResult(f"q{i}", "execution_error", "", error="E" * 5000) for i in range(20)
        ]
        result = RunResult("c", "id", "v", tuple(failures_mass), 0)
        baseline = EvaluationResult({"m": 0}, 20, 0, 20, 0, huge_rows)
        feedback = training_feedback([C()], [result], [("c", huge_rows)], baseline)
        self.assertNotIn("diagnostics", feedback["scores"])
        # 上限以完整序列化载荷为准（含骨架/字段名/统计），不得放宽
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        self.assertTrue(feedback["generation_failures_truncated"])
        self.assertEqual(feedback["generation_failures_total"], 20)

    def test_composite_question_identity_in_admission(self):

        from tests.support.device import spec

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        s = spec(root / "assets")
        a = s.bundle.get("answer_prompt")
        composite = [training_id("case-a", "q1"), training_id("case-b", "q1")]
        svc = AssetRevisionService()
        # 不可解析（裸 id / 无长度前缀）与可解析但不存在的依据都被拒绝
        for bad in (["q1"], ["case-a::q1"], [training_id("case-a", "qX")]):
            with self.assertRaises(ValueError):
                svc.propose(
                    s.bundle,
                    [AssetPatch(replace(a, content="Be precise."), a.fingerprint, "r", tuple(bad))],
                    root / "cand",
                    composite,
                )
        good = svc.propose(
            s.bundle,
            [
                AssetPatch(
                    replace(a, content="Be precise."),
                    a.fingerprint,
                    "r",
                    (training_id("case-b", "q1"),),
                )
            ],
            root / "cand2",
            composite,
        )
        self.assertNotEqual(good.version, s.bundle.version)

    def test_stage_statuses_per_case_layout(self):
        from tests.support.recorded_campaign import RecordedCampaign, protocol

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        v1 = root / "validation" / "v1"
        for case, status in (("val-case", "complete"), ("val-case-2", "failed")):
            d = v1 / case
            d.mkdir(parents=True)
            (d / "stage.json").write_text(json.dumps({"status": status}))
        statuses = controller._stage_statuses()
        self.assertEqual(statuses["validation/v1/val-case"]["status"], "complete")
        unhealthy = {k: v for k, v in statuses.items() if v["status"] != "complete"}
        self.assertEqual(list(unhealthy), ["validation/v1/val-case-2"])
        self.assertEqual(unhealthy["validation/v1/val-case-2"]["version"], "v1")
        self.assertEqual(unhealthy["validation/v1/val-case-2"]["case"], "val-case-2")
