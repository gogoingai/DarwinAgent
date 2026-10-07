"""Offline regression scenarios for feedback budget."""

import json
import unittest

from darwinagent.contracts import (
    EvaluationResult,
    SourceRef,
)


class GenericFeedbackContract(unittest.TestCase):
    def test_dataset_specific_diagnostics_flow_through(self):
        from darwinagent.contracts import AnswerResult, RunResult
        from darwinagent.experiments.feedback import training_feedback

        # 旅行式诊断（预算/人数/约束），框架不得丢弃或改读 LoCoMo 字段
        rows = (
            {
                "query_id": "q7",
                "budget_exceeded": True,
                "people": 3,
                "constraint": "no flight",
                "plan_issues": ["超预算"],
            },
        )
        ev = (SourceRef("message_text", "c", "7"),)

        class C:
            id = "c"

        result = RunResult("c", "id", "v", (AnswerResult("q7", "answered", "ok", evidence=ev),), 5)
        baseline = EvaluationResult({"feasible": 0, "budget_ok": 1}, 1, 1, 0, 0, rows)
        feedback = training_feedback([C()], [result], [("c", rows)], baseline)
        self.assertEqual(feedback["diagnostics"][0]["case_id"], "c")
        self.assertEqual(feedback["diagnostics"][0]["diagnostic"]["constraint"], "no flight")
        self.assertEqual(feedback["diagnostic_rows_total"], 1)
        self.assertIn("feasible", feedback["scores"]["metrics"])

    def test_passed_rows_skipped_and_budget_capped(self):
        from darwinagent.contracts import RunResult
        from darwinagent.experiments.feedback import FEEDBACK_BUDGET_CHARS, training_feedback

        rows = tuple({"i": i, "passed": True, "payload": "x"} for i in range(3)) + (
            {"i": 9, "payload": "y" * 100},
        )
        big = {"i": 10, "payload": "z" * (FEEDBACK_BUDGET_CHARS + 10)}
        rows = rows + (big,)

        class C:
            id = "c"

        result = RunResult("c", "id", "v", (), 0)
        baseline = EvaluationResult({"m": 0}, 0, 0, 0, 0, rows)
        feedback = training_feedback([C()], [result], [("c", rows)], baseline)
        # 新契约（评审#2）：未识别结构的超长行压缩入载（_row_truncated 前缀），总预算仍受控
        self.assertEqual(
            [r["diagnostic"]["i"] for r in feedback["diagnostics"] if "i" in r["diagnostic"]], [9]
        )
        self.assertTrue(any("_row_truncated" in r["diagnostic"] for r in feedback["diagnostics"]))
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        # 新契约：计数只含失败行（3 条 passed 不计），9 号与压缩后的超长行共 2 条
        self.assertEqual(feedback["diagnostic_rows_total"], 2)
