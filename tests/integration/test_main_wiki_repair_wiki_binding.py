"""Offline regression scenarios for wiki binding."""

import unittest

from darwinagent.experiments.wiki_lessons import _lessons
from tests.support import wiki_events as helper


class ColonCaseBindingTests(unittest.TestCase):
    def lessons(self, target_case="train:0", target_scenario="stress", target_graph="g1"):

        h = helper.INPUT_DIGEST
        return _lessons(
            [
                helper.failed_attempt(f"train:0:stress:0:{h}", graph_digests={"train:0": "g1"}),
                helper.passed_attempt(
                    [
                        {
                            "asset_id": "f_flight_pair",
                            "scenario_id": target_scenario,
                            "status": "passed",
                            "required": True,
                            "input_ref": f"{target_case}:{target_scenario}:0:{h}",
                        }
                    ],
                    graph_digests={target_case: target_graph},
                ),
            ]
        )

    def test_colon_case_different_scenario_graph_or_case_cannot_verify(self):
        for kwargs in (
            {"target_scenario": "base"},
            {"target_graph": "g2"},
            {"target_case": "train:1"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(
                    any(
                        lesson["status"] == "admission_verified"
                        for lesson in self.lessons(**kwargs)
                    )
                )

    def test_matching_colon_case_verifies_actual_reproduction(self):
        self.assertTrue(any(lesson["status"] == "admission_verified" for lesson in self.lessons()))
