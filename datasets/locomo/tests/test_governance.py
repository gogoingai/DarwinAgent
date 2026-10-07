"""Governance must not authorize historical source or converge domain scoring."""

import json
import tempfile
import unittest
from datetime import date

from darwinagent.operators import calendar
from darwinagent.operators import dates as framework_dates
from darwinagent.runtime.artifacts import verify_files
from datasets.locomo.evaluator import LOCK_PATH, ROOT, LocomoEvaluator
from datasets.locomo.pipeline import dates as task_dates


class GovernanceEvaluation(unittest.TestCase):
    def test_new_lock_covers_scoring_dependencies_and_old_lock_rejects_source(self):
        lock = json.loads(LOCK_PATH.read_text())
        original = ROOT / "datasets/locomo/evaluation_lock.json"
        self.assertTrue(set(json.loads(original.read_text())).issubset(lock))
        self.assertIn("src/darwinagent/operators/calendar.py", lock)
        self.assertIn("datasets/locomo/pipeline/dates.py", lock)
        verify_files(ROOT, lock)
        with self.assertRaisesRegex(ValueError, "Frozen files changed"):
            verify_files(ROOT, json.loads(original.read_text()))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(LocomoEvaluator(None, tmp).lock_path, LOCK_PATH)
            self.assertEqual(LocomoEvaluator(None, tmp, lock_path=original).lock_path, original)

    def test_calendar_edges_and_domain_scoring_difference(self):
        self.assertEqual(calendar._shift_month(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(calendar._shift_month(date(2023, 3, 31), -1), date(2023, 2, 28))
        self.assertIsNone(calendar.parse_session_datetime("31 February, 2024"))
        self.assertEqual(calendar.parse_session_datetime("8 May, 2023"), date(2023, 5, 8))
        for module in (framework_dates, task_dates):
            self.assertEqual(
                module.resolve_relative(date(2024, 3, 1), "昨天"), ("2024-02-29", "日")
            )
            self.assertEqual(module.resolve_relative(date(2024, 3, 1), "去年"), ("2023", "年"))
            self.assertIs(module.normalize_answer_text, calendar.normalize_answer_text)
        self.assertTrue(framework_dates.answer_equivalent("2023年5月", "2023年5月7日"))
        self.assertFalse(task_dates.answer_equivalent("2023年5月", "2023年5月7日"))
