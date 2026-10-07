"""Offline regression scenarios for validation selection."""

import unittest

from darwinagent.experiments.admission_samples import _traversal_shape
from darwinagent.kernel.assets import Asset


class SelectorSliceRegression(unittest.TestCase):
    def test_row_slice_limit_is_not_external_seed(self):
        asset = Asset(
            "f",
            "F",
            "def run(params):\n rows=nodes(limit=50)\n bounded=rows[:params['limit']]\n ids=[]\n for r in bounded:\n  ids.append(r['node_id'])\n return traverse(ids,'related')",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
            {"type": "array"},
            ["schema"],
            trial_inputs=({"limit": 3},),
        )
        self.assertEqual(_traversal_shape(asset)[0], set())

    def test_external_seed_slice_preserves_only_identity_parameter(self):
        asset = Asset(
            "f",
            "F",
            "def run(params):\n ids=params['seed_ids'][:params['limit']]\n return traverse(ids,'related')",
            {
                "type": "object",
                "properties": {
                    "seed_ids": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
            },
            {"type": "array"},
            ["schema"],
            trial_inputs=({"seed_ids": ["n000001"], "limit": 1},),
        )
        self.assertEqual(_traversal_shape(asset)[0], {"seed_ids"})
