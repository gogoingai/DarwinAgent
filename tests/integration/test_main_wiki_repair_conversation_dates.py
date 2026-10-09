"""Offline regression scenarios for conversation dates."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.graph_rules import _session_dates


class ConversationDateTests(unittest.TestCase):
    def setUp(self):
        from tests.support.locomo import fixture_dataset

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.data_path = fixture_dataset(holder.name) / "locomo10_zh.json"

    def test_all_session_dates_follow_message_metadata(self):
        case = LocomoAdapter(self.data_path).generation_input("conv-26")
        from datasets.locomo.graph_rules import load_facts

        facts, _ = load_facts(Path("tests/fixtures/locomo_snapshot/conv-26"))
        dates = _session_dates(facts, case.corpus)
        self.assertEqual(dates[1], "2023-05-08")
        for block in case.corpus:
            n = int(block.source.location.split(":")[0][1:])
            self.assertEqual(dates[n], block.metadata["date"][:10])
        altered = [{**f, "date_iso": "1999-01-01"} for f in facts]
        self.assertEqual(_session_dates(altered, case.corpus), dates)

    def test_missing_or_conflicting_record_dates_are_not_event_dates(self):
        facts = [{"fid": "x", "session_no": 1, "date_iso": "2023-05-07"}]
        self.assertEqual(_session_dates(facts), {})
        case = LocomoAdapter(self.data_path).generation_input("conv-26")
        block = case.corpus[0]
        conflict = replace(block, metadata={**block.metadata, "date": "1999-01-01"})
        with self.assertRaisesRegex(ValueError, "日期冲突"):
            _session_dates(facts, (block, conflict))
