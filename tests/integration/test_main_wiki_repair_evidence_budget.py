"""Offline regression scenarios for evidence budget."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from datasets.locomo.scripts import question_split


class WholeEvidenceGroupTests(unittest.TestCase):
    def test_actual_default_split_and_seeds_have_no_shared_evidence(self):
        qs = next(
            q for q in json.loads(question_split.DATA.read_text()) if q["sample_id"] == "conv-26"
        )["qa"]
        for seed in (20261005, 0, 1, 42):
            with self.subTest(seed=seed):
                split = question_split.build_split(seed=seed)
                self.assertEqual((len(split["train"]), len(split["validation"])), (15, 10))
                train = {e for i in split["train"] for e in qs[i].get("evidence") or ()}
                val = {e for i in split["validation"] for e in qs[i].get("evidence") or ()}
                self.assertFalse(train & val)
                self.assertEqual(split, question_split.build_split(seed=seed))

    def test_transitive_evidence_groups_across_categories_stay_whole(self):
        qs = [
            {"category": 1, "evidence": ["A"]},
            {"category": 2, "evidence": ["A", "B"]},
            {"category": 3, "evidence": ["B"]},
            {"category": 1, "evidence": ["C"]},
            {"category": 3, "evidence": ["C"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            path.write_text(json.dumps([{"sample_id": "recorded", "qa": qs}]))
            with mock.patch.object(question_split, "DATA", path):
                split = question_split.build_split("recorded", train_n=3, val_n=2)
                self.assertEqual(split["train"], [0, 1, 2])
                self.assertEqual(split["validation"], [3, 4])
                with self.assertRaisesRegex(ValueError, "without splitting"):
                    question_split.build_split("recorded", train_n=2, val_n=2)
