"""Recorded stability regressions for capability."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    QuestionInput,
)
from darwinagent.experiments.admission import AdmissionError, _samples
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.experiments.snapshots import load_frozen_graph
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.functions import FunctionRegistry
from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import Limits
from tests.support.graphs import (
    build_snapshot,
    cold_bundle,
    corpus,
    gvtest_graph,
)
from tests.support.preflight import preflight_sync


class CapabilityTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_date_object_and_projection_keep_returned_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            seed = cold_bundle(root / "seed", with_c=False)
            base = next(a for a in seed.assets.assets if a.kind == "F")
            function = replace(
                base,
                id="f_date_projection",
                content="def run(params):\n"
                " date=relative_date(params['anchor_iso'],params['expression'])\n"
                " rows=nodes('原子事实',limit=2)\n"
                " return {'date':date,'rows':project(rows,['node_id','source_ids'])}\n",
                input_contract={
                    "type": "object",
                    "properties": {
                        "anchor_iso": {"type": "string"},
                        "expression": {"type": "string"},
                    },
                    "required": ["anchor_iso", "expression"],
                },
                output_contract={
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "object",
                            "properties": {
                                "anchor": {"type": "string"},
                                "expression": {"type": "string"},
                                "resolved": {"type": "string"},
                                "granularity": {"type": "string"},
                            },
                            "required": ["anchor", "expression", "resolved", "granularity"],
                        },
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "node_id": {"type": "string"},
                                    "source_ids": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["node_id", "source_ids"],
                            },
                        },
                    },
                    "required": ["date", "rows"],
                },
                trial_inputs=({"anchor_iso": "2024-05-08", "expression": "上周日"},),
            )
            bundle = KernelAssets(
                tuple(a for a in seed.assets.assets if a.kind != "F") + (function,)
            ).export(root / "repaired")
            outcome = FunctionRegistry(bundle, Limits(30000, 15, 180000)).call(
                function.id, {"anchor_iso": "2024-05-08", "expression": "上周日"}, graph
            )
            self.assertEqual(
                set(outcome["data"]["date"]), {"anchor", "expression", "resolved", "granularity"}
            )
            self.assertTrue(outcome["data"]["date"]["resolved"])
            self.assertEqual(outcome["data"]["date"]["anchor"], "2024-05-08")
            self.assertEqual(
                outcome["node_ids"], sorted(row["node_id"] for row in outcome["data"]["rows"])
            )
            self.assertEqual(outcome["node_ids"], outcome["read_node_ids"])
            self.assertTrue(outcome["source_ids"])
            self.assertEqual(outcome["capability_calls"]["relative_date"], 1)
            self.assertEqual(outcome["capability_calls"]["project"], 1)
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "new-run",
                bootstrap_trial_graph=graph,
            )
            report = self._preflight_sync(
                runner,
                bundle,
                SimpleNamespace(retrieval_floor={}),
                QuestionInput("q1", "上周日的事实"),
            )
            self.assertEqual(report["verdict"], "passed")
            self.assertTrue(
                any(
                    row["scenario_id"] == "relative_date_object"
                    and row["status"] == "passed"
                    and row["capability_calls"].get("relative_date") == 1
                    for row in report["scenarios"]
                )
            )

    def test_high_degree_samples_use_public_row_ids(self):
        graph = gvtest_graph()
        start, end = list(graph.nodes)[:2]
        graph.add_edge(start, end, relation="相关")
        view = SimpleNamespace(graph=graph)
        asset = Asset(
            "f_expand",
            "F",
            "def run(params):\n"
            " return traverse(params['rows'][0]['node_id'], '相关', params['direction'])\n",
            {
                "type": "object",
                "properties": {
                    "rows": {"type": "array"},
                    "direction": {"type": "string", "enum": ["in", "out"]},
                },
                "required": ["rows", "direction"],
            },
            {"type": "array"},
            ["schema"],
            trial_inputs=({"rows": [], "direction": "out"},),
        )
        by_tag = dict(_samples(asset, view))
        self.assertIn("high_degree_out", by_tag)
        self.assertIn("high_degree_in", by_tag)
        public_ids = set(DataCapabilities(view).rows)
        self.assertIn(by_tag["high_degree_out"]["rows"][0]["node_id"], public_ids)
        self.assertIn(by_tag["high_degree_in"]["rows"][0]["node_id"], public_ids)
        caps = DataCapabilities(view)
        for direction in ("in", "out"):
            params = by_tag["high_degree_" + direction]
            self.assertTrue(caps.traverse(params["rows"][0]["node_id"], "相关", direction))
        parameterized = replace(
            asset,
            content="def run(params):\n"
            " relation=params.get('relation','相关')\n"
            " return traverse(params['rows'][0]['node_id'],relation=relation,"
            "direction=params['direction'])\n",
        )
        by_tag = dict(_samples(parameterized, view))
        for direction in ("in", "out"):
            params = by_tag["high_degree_" + direction]
            self.assertTrue(caps.traverse(params["rows"][0]["node_id"], "相关", direction))

    def test_high_degree_empty_traversal_is_not_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            seed = cold_bundle(root / "seed", with_c=False)
            function = Asset(
                "f_empty",
                "F",
                "def run(params):\n"
                " relation='不存在'+params['direction']\n"
                " return traverse(params['node_id'],relation,params['direction'])\n",
                {
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "direction": {"type": "string", "enum": ["in", "out"]},
                    },
                    "required": ["node_id", "direction"],
                },
                {"type": "array"},
                ["schema"],
                trial_inputs=({"node_id": "n000000", "direction": "out"},),
            )
            bundle = KernelAssets(
                tuple(a for a in seed.assets.assets if a.kind != "F") + (function,)
            ).export(root / "candidate")
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "run",
                bootstrap_trial_graph=graph,
            )
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner, bundle, SimpleNamespace(retrieval_floor={}), QuestionInput("q1", "事实")
                )
            report = json.loads((root / "admission.json").read_text())
            for direction in ("in", "out"):
                self.assertTrue(
                    any(
                        row["scenario_id"] == "high_degree_" + direction
                        and row["status"] == "incomplete"
                        and row["error_type"] == "CoverageGap"
                        and row["traverse_observations"][0]["matched_edges"] == 0
                        for row in report["scenarios"]
                    )
                )
