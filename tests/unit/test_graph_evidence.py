"""Graph diagnostics exposed as evidence, without assuming the cause of answer errors."""

import unittest

from darwinagent.schema.model import Schema
from datasets.locomo.graph_rules import rebuild_graph
from tests.support.locomo import MINIMAL_S


class GraphEvidenceTests(unittest.TestCase):
    def test_records_missing_structure_and_version_delta(self):
        from darwinagent.experiments.graph_evidence import graph_evidence

        schema = Schema.from_yaml(MINIMAL_S)
        facts = [{"fid": "f1", "statement": "小林买了自行车", "subject": "小林", "sources": []}]
        graph = rebuild_graph(facts, schema)
        evidence = graph_evidence(graph, schema)
        self.assertIn("主题", evidence["unmaterialized_types"])
        self.assertIn("属于主题", evidence["unmaterialized_relations"])
        self.assertIn("graph_digest", evidence)
        self.assertEqual(evidence["causal_status"], "observation_only")
        changed = graph.copy()
        changed.remove_edges_from(list(changed.edges(keys=True)))
        newer = graph_evidence(changed, schema, previous=evidence)
        self.assertEqual(newer["delta"]["edges"], -1)
        self.assertIn("f1", newer["facts_without_relations"])

    def test_safe_feedback_keeps_graph_evidence(self):
        from darwinagent.experiments.wiki_evidence import safe_feedback

        result = safe_feedback({"scores": {}, "graphs": [{"case_id": "c", "nodes": 2}]})
        self.assertEqual(result["graphs"], [{"case_id": "c", "nodes": 2}])

    def test_four_asset_signals_are_hypotheses_with_evidence(self):
        from darwinagent.experiments.graph_evidence import asset_change_signals

        feedback = {
            "graphs": [{"case_id": "c", "graph_digest": "g", "unmaterialized_types": ["物品"]}],
            "diagnostics": [
                {
                    "case_id": "c",
                    "diagnostic": {"question_id": "q", "precise": False},
                    "trace": {
                        "tool_errors": [{"asset_id": "f_find", "error": "bad parameter"}],
                        "rejections": [{"check_id": "c_shape", "issues": "rejected"}],
                        "returned_rows": 3,
                    },
                }
            ],
        }
        signals = asset_change_signals(feedback)
        self.assertEqual({kind for s in signals for kind in s["asset_kinds"]}, {"S", "F", "C", "P"})
        self.assertTrue(
            all(
                s["confidence"] == "hypothesis" and s["evidence"] and s["next_check"]
                for s in signals
            )
        )

    def test_graph_failure_is_visible_before_answers_exist(self):
        from darwinagent.experiments.graph_evidence import asset_change_signals

        signals = asset_change_signals(
            {"graph_diagnostics": [{"status": "execution_error", "error": "undeclared type"}]}
        )
        self.assertIn("S", signals[0]["asset_kinds"])
        self.assertIn("P", signals[0]["asset_kinds"])
