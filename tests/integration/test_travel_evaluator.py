import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.contracts import AnswerResult, RunResult
from datasets.travelplanner.evaluator import TravelPlannerEvaluator
from datasets.travelplanner.pipeline.eval.adapter import SubsetScores


class TravelEvaluationIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        row = {
            "idx": 7,
            "org": "Independent Origin",
            "dest": "Independent Destination",
            "days": 3,
            "date": ["2022-01-01"],
            "people_number": 1,
            "local_constraint": {},
            "budget": 500,
            "query": "Independent request",
            "level": "easy",
            "visiting_city_number": 1,
            "reference_information": {},
        }
        (self.data_dir / "train.queries.jsonl").write_text(json.dumps(row) + "\n")
        with patch("datasets.travelplanner.evaluator.OfficialEvaluator"):
            self.evaluator = TravelPlannerEvaluator(
                self.data_dir / "evaluation", data_dir=self.data_dir
            )
        self.result = RunResult(
            "train:7",
            "identity",
            "assets",
            (AnswerResult("7", "execution_error", "", error="generation failed"),),
            0,
        )

    def test_reads_explicit_independent_reference_directory(self):
        self.evaluator.official.eval_subset.return_value = SubsetScores(n=1, per_query=[{"idx": 7}])
        score = asyncio.run(self.evaluator.evaluate(self.result))
        queries, plans = self.evaluator.official.eval_subset.call_args.args
        self.assertEqual(queries[0].org, "Independent Origin")
        self.assertEqual(plans, [[]])
        self.assertEqual(
            (score.total, score.completed, score.generation_faults, score.evaluation_faults),
            (1, 1, 1, 0),
        )

    def test_missing_reference_does_not_download_or_fall_back(self):
        (self.data_dir / "train.queries.jsonl").unlink()
        with self.assertRaises(FileNotFoundError):
            asyncio.run(self.evaluator.evaluate(self.result))
        self.evaluator.official.eval_subset.assert_not_called()

    def test_relative_runtime_paths_are_resolved_before_worker_changes_directory(self):
        with patch("datasets.travelplanner.evaluator.OfficialEvaluator"):
            evaluator = TravelPlannerEvaluator(
                "relative-evaluation", tp_root="relative-official", data_dir="relative-references"
            )
        self.assertEqual(evaluator.cfg.work_dir, Path("relative-evaluation").resolve())
        self.assertEqual(evaluator.cfg.tp_root, Path("relative-official").resolve())
        self.assertEqual(evaluator.data_dir, Path("relative-references").resolve())

    def test_worker_failure_remains_evaluation_failure(self):
        self.evaluator.official.eval_subset.side_effect = RuntimeError("worker failed")
        score = asyncio.run(self.evaluator.evaluate(self.result))
        self.assertEqual((score.completed, score.evaluation_faults), (0, 1))
        self.assertIn("worker failed", score.diagnostics[0]["error"])

    def test_truncated_official_result_is_not_complete(self):
        self.evaluator.official.eval_subset.return_value = SubsetScores(n=1, per_query=[])
        score = asyncio.run(self.evaluator.evaluate(self.result))
        self.assertEqual((score.completed, score.evaluation_faults), (0, 1))

    def test_mismatched_case_answer_rejected(self):
        wrong = RunResult("train:8", "identity", "assets", self.result.answers, 0)
        with self.assertRaises(ValueError):
            asyncio.run(self.evaluator.evaluate(wrong))
        self.evaluator.official.eval_subset.assert_not_called()
