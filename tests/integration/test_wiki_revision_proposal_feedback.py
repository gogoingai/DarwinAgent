"""Offline regression scenarios for proposal feedback."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.kernel.assets import Asset, KernelAssets
from tests.support.graphs import cold_bundle


class BootstrapFeedbackRegression(unittest.TestCase):
    def test_unsupported_union_types_are_reported_together_before_data_execution(self):
        from darwinagent.kernel.validation import validate_bundle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = cold_bundle(root / "seed", with_c=False)
            f = Asset(
                "f_bad_contract",
                "F",
                "def run(params):\n return []",
                {"type": "object", "properties": {}},
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": ["string", "null"]},
                            "日期": {"type": ["string", "null"]},
                        },
                        "additionalProperties": True,
                    },
                },
                ["schema"],
                trial_inputs=({},),
            )
            bundle = KernelAssets(
                tuple(a for a in seed.assets.assets if a.kind != "F") + (f,)
            ).export(root / "candidate")
            with self.assertRaises(ValueError) as caught:
                validate_bundle(bundle)
            self.assertIn("node_id", str(caught.exception))
            self.assertIn("日期", str(caught.exception))
            self.assertIn("not a union/list", str(caught.exception))

    def test_resource_feedback_contains_real_input_steps_and_matched_edges(self):
        from darwinagent.experiments.bootstrap import _trial_failure_feedback

        report = {
            "scenarios": [
                {
                    "asset_id": "f_expand",
                    "scenario_id": "high_degree_in",
                    "status": "failed",
                    "required": True,
                    "error_type": "SandboxError",
                    "error": "Restricted execution budget exhausted",
                    "parameters": {"node_ids": ["n000114"]},
                    "steps_used": 30001,
                    "step_budget": 30000,
                    "traverse_observations": [
                        {"direction": "in", "relation": "属于主题", "matched_edges": 145}
                    ],
                }
            ]
        }
        feedback = _trial_failure_feedback(report)
        for value in ("n000114", "30001", "30000", "145", "昂贵处理结束后才截断"):
            self.assertIn(value, feedback)
