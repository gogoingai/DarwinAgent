"""Offline regression scenarios for feedback isolation."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.contracts import (
    CorpusBlock,
    EvaluationResult,
    SourceRef,
)
from darwinagent.kernel.revision import (
    parse_training_id,
    training_id,
)
from tests.support.evaluators import StageTaggedEvaluator


class FeedbackIsolationTests(unittest.TestCase):
    """P1 proposal diagnostics follow the last ADOPTED stage; P2 the complete serialized
    payload respects the budget; P2 the training identity encoding is collision-free."""

    @staticmethod
    def feedback_for(root, stage):
        return json.loads((root / stage / "proposal-call.json").read_text())["input"][
            "task_training_feedback"
        ]

    def run_rounds(self, root, rounds, resume=False, runner_cls=None):
        import contextlib
        import io

        from darwinagent.kernel import TaskSpec
        from tests.support.device import TASK
        from tests.support.recorded_experiment import RecordedExperiment

        cls = runner_cls or RecordedExperiment
        runner = cls(root, evaluator=lambda transport, path: StageTaggedEvaluator(path))
        spec = TaskSpec.load(TASK / "task.yaml")
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(
                runner.run(runner.case.id, spec, rounds=rounds, resume=resume)
            ), runner

    def test_proposal_diagnostics_follow_last_adopted(self):
        # 完整提案流程（非单测 training_feedback）：R1 的诊断来自 B0；R1 采纳后 R2 来自 R1；
        # R2 被拒后 R3 仍来自 R1。提案输入的题目身份可解码回原题。
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        summary, runner = self.run_rounds(root, 3)
        self.assertEqual([d["accepted"] for d in summary["rounds"]], [True, False, False])
        r1 = self.feedback_for(root, "R1")
        self.assertEqual([r["diagnostic"]["stage_tag"] for r in r1["diagnostics"]], ["B0"])
        self.assertEqual(r1["diagnostic_rows_total"], 1)
        r2 = self.feedback_for(root, "R2")
        self.assertEqual([r["diagnostic"]["stage_tag"] for r in r2["diagnostics"]], ["R1"])
        r3 = self.feedback_for(root, "R3")
        self.assertEqual([r["diagnostic"]["stage_tag"] for r in r3["diagnostics"]], ["R1"])
        questions = json.loads((root / "R1" / "proposal-call.json").read_text())["input"][
            "questions"
        ]
        self.assertEqual(
            parse_training_id(questions[0]["training_id"]),
            (runner.case.id, runner.case.questions[0].id),
        )

    def test_resume_keeps_diagnostic_source(self):
        from tests.support.recorded_experiment import RecordedExperiment

        class CrashedBeforeR3(RecordedExperiment):
            """First process dies right before the R3 proposal; resume must replay history."""

            armed = True

            def _client(self, stage):
                if self.armed and stage == "R3":
                    raise RuntimeError("中断：R3 提案前")
                return super()._client(stage)

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        with self.assertRaises(RuntimeError):
            self.run_rounds(root, 3, runner_cls=CrashedBeforeR3)
        again, _ = self.run_rounds(root, 3, resume=True)
        self.assertEqual([d["accepted"] for d in again["rounds"]], [True, False, False])
        # 恢复后新提案（R3）的诊断仍来自最后采纳版本 R1，而非尚未评测的 R3
        r3 = self.feedback_for(root, "R3")
        self.assertEqual([r["diagnostic"]["stage_tag"] for r in r3["diagnostics"]], ["R1"])

    def test_many_short_records_bounded_by_complete_payload(self):
        from darwinagent.contracts import RunResult
        from darwinagent.experiments.feedback import FEEDBACK_BUDGET_CHARS, training_feedback

        class C:
            id = "c"

        rows = tuple({"i": i, "payload": "x" * 60} for i in range(3000))
        baseline = EvaluationResult({"m": 0}, 0, 0, 0, 0, rows)
        feedback = training_feedback(
            [C()], [RunResult("c", "id", "v", (), 0)], [("c", rows)], baseline
        )
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        self.assertGreater(feedback["diagnostic_rows_in_proposal"], 0)
        self.assertEqual(feedback["diagnostic_rows_total"], 3000)

    def test_few_long_records_bounded_by_complete_payload(self):
        from darwinagent.contracts import RunResult
        from darwinagent.experiments.feedback import FEEDBACK_BUDGET_CHARS, training_feedback

        class C:
            id = "c"

        rows = tuple({"i": i, "payload": "y" * 20000} for i in range(8))
        baseline = EvaluationResult({"m": 0}, 0, 0, 0, 0, rows)
        feedback = training_feedback(
            [C()], [RunResult("c", "id", "v", (), 0)], [("c", rows)], baseline
        )
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        # 新契约（评审#2）：超长未识别结构行压缩入载，8 条全部可进且总预算受控
        self.assertEqual(feedback["diagnostic_rows_in_proposal"], 8)
        self.assertEqual(feedback["diagnostic_rows_total"], 8)
        self.assertTrue(all("_row_truncated" in r["diagnostic"] for r in feedback["diagnostics"]))

    def test_mixed_sections_bounded_by_complete_payload(self):
        from darwinagent.contracts import AnswerResult, RunResult
        from darwinagent.experiments.feedback import FEEDBACK_BUDGET_CHARS, training_feedback

        class C:
            id = "c"

        rows = tuple({"i": i, "payload": "d" * 300} for i in range(200))
        failures = tuple(
            AnswerResult(f"q{i}", "execution_error", "", error="E" * 400) for i in range(30)
        )
        graph_rows = tuple({"g": i, "detail": "G" * 200} for i in range(50))
        result = RunResult("c", "id", "v", failures, 0, graph_diagnostics=graph_rows)
        baseline = EvaluationResult({"m": 0}, 30, 0, 30, 0, rows)
        feedback = training_feedback([C()], [result], [("c", rows)], baseline)
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        # 优先级保持：诊断先填满，之后才轮到故障与图诊断
        self.assertGreater(feedback["diagnostic_rows_in_proposal"], 0)
        self.assertLess(feedback["diagnostic_rows_in_proposal"], 200)
        self.assertTrue(feedback["generation_failures_truncated"])
        self.assertEqual(feedback["generation_failures_total"], 30)

    def test_training_id_is_collision_free_and_parseable(self):
        # 评审给出的两个合法输入：旧的 'a::b' 拼接会碰撞，长度前缀编码必须区分并精确还原
        a = training_id("case-a::part", "q1")
        b = training_id("case-a", "part::q1")
        self.assertNotEqual(a, b)
        self.assertEqual(parse_training_id(a), ("case-a::part", "q1"))
        self.assertEqual(parse_training_id(b), ("case-a", "part::q1"))
        for bad in ("q1", "case-a::q1", "6:case-a::", "x:case-a::q1", "99:case-a::q1", ""):
            with self.assertRaises(ValueError):
                parse_training_id(bad)

    def test_question_identity_uses_the_encoder(self):
        from darwinagent.contracts import CaseInput, QuestionInput
        from darwinagent.experiments.feedback import question_identity

        b = CorpusBlock(SourceRef("message_text", "c", "1"), "文本。")
        case = CaseInput(
            "case-a", (b,), (QuestionInput("q::1", "问题"), QuestionInput("q1", "问题2"))
        )
        ids = question_identity(case)
        self.assertEqual(
            [parse_training_id(t) for t in ids], [("case-a", "q::1"), ("case-a", "q1")]
        )
