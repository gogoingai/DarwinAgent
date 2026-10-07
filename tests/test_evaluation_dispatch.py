"""Verify orchestration preserves the existing frozen evaluator numerically."""

import asyncio
import unittest
from unittest.mock import patch

from datasets.locomo.pipeline import judge
from datasets.locomo.pipeline.data import QA
from datasets.locomo.pipeline.evaluation_dispatch import grade_all_dispatched


class EvaluationDispatchContract(unittest.TestCase):
    def test_dispatch_matches_original_report(self):
        qas = [
            QA(0, "q", 4, "answer"),
            QA(1, "q", 4, "other"),
            QA(2, "q", 5, None),
            QA(3, "q", 1, "failed"),
        ]
        preds = {0: "answer", 1: "longer", 2: "对话中未提及该信息", 3: ""}
        statuses = {3: "answer_error"}

        async def fake_grade(*args, **kwargs):
            return {"grade": "partial", "missing": ["element"], "reason": "missing"}

        async def run():
            return (
                await judge.grade_all(
                    qas, preds, None, "synthetic", repairs={}, answer_statuses=statuses
                ),
                await grade_all_dispatched(qas, preds, None, "synthetic", statuses),
            )

        with (
            patch.object(judge, "load_repairs", return_value={}),
            patch.object(judge, "llm_grade", fake_grade),
        ):
            sequential, concurrent = asyncio.run(run())
        self.assertEqual(sequential, concurrent)


if __name__ == "__main__":
    unittest.main()
