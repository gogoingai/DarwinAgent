"""Offline regression scenarios for capability."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, GraphResult, QuestionInput
from darwinagent.experiments.admission import (
    AdmissionError,
    _pressure_graph,
    admit_candidate,
)
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.functions import FunctionRegistry
from tests.support.graphs import cold_bundle, corpus, gvtest_graph


class InterfaceAdmissionRegression(unittest.TestCase):
    def graph(self):
        return GraphResult(gvtest_graph(), {b.source.id: b for b in corpus()})

    def bundle(self, root, asset):
        seed = cold_bundle(root / "seed", with_c=False)
        return KernelAssets(
            tuple(a for a in seed.assets.assets if a.kind != "F") + (asset,)
        ).export(root / "candidate")

    def admit(self, bundle, graph, path, config=None):
        case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "有哪些事实？"),))
        return admit_candidate(bundle, [case], {"conv-x": graph}, config or RunConfig(), (), path)

    def test_seed_direction_aliases_receive_identical_required_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = []
            outputs = []
            for index, (seed, direction) in enumerate(
                (("node_id", "direction"), ("seed_id", "orientation"), ("种子", "方向"))
            ):
                root = Path(tmp) / str(index)
                f = Asset(
                    "f_expand",
                    "F",
                    f"def run(params):\n return traverse(params['{seed}'],'归属于',params['{direction}'])",
                    {
                        "type": "object",
                        "properties": {
                            seed: {"type": "string"},
                            direction: {"type": "string", "enum": ["in", "out"]},
                        },
                        "required": [seed, direction],
                    },
                    {"type": "array"},
                    ["schema"],
                    trial_inputs=({seed: "n000000", direction: "out"},),
                )
                bundle = self.bundle(root, f)
                graph = self.graph()
                outputs.append(
                    FunctionRegistry(bundle).call(f.id, {seed: "n000000", direction: "out"}, graph)[
                        "node_ids"
                    ]
                )
                reports.append(self.admit(bundle, graph, root / "admission.json"))
            self.assertEqual(outputs, [["n000002"]] * 3)
            for report in reports:
                self.assertEqual(report["verdict"], "passed")
                for direction in ("in", "out"):
                    self.assertTrue(
                        any(
                            s["scenario_id"] == "high_degree_" + direction
                            and s["required"]
                            and s["status"] == "passed"
                            for s in report["scenarios"]
                        )
                    )

    def test_internal_seed_uses_copy_and_reaches_high_degree_before_limit(self):
        graph = self.graph()
        g = graph.graph.copy()
        nodes = list(g.nodes)
        g.add_edge(nodes[1], nodes[2], relation="归属于")
        graph = replace(graph, graph=g)
        f = Asset(
            "f_internal",
            "F",
            "def run(params):\n rows=nodes(entity_type='原子事实',limit=1)\n return traverse(rows[0]['node_id'],'归属于',params['方向'])",
            {
                "type": "object",
                "properties": {"方向": {"type": "string", "enum": ["out"]}},
                "required": ["方向"],
            },
            {"type": "array"},
            ["schema"],
            trial_inputs=({"方向": "out"},),
        )
        original = list(g.nodes)
        pressure = _pressure_graph(f, {"方向": "out"}, graph, "high_degree_out")
        self.assertEqual(list(g.nodes), original)
        self.assertEqual(set(g.nodes), set(pressure.graph.nodes))
        self.assertEqual(set(g.edges(keys=True)), set(pressure.graph.edges(keys=True)))
        self.assertEqual(list(pressure.graph.nodes)[0], nodes[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root, f)
            report = self.admit(bundle, graph, root / "admission.json")
            stress = next(s for s in report["scenarios"] if s["scenario_id"] == "high_degree_out")
            self.assertEqual(stress["pressure_kind"], "graph_row_order")
            self.assertEqual(stress["traverse_observations"][0]["matched_edges"], 2)
            self.assertEqual(report["verdict"], "passed")

    def test_one_empty_selector_does_not_invalidate_a_covered_pressure_path(self):
        f = Asset(
            "f_internal",
            "F",
            "def run(params):\n if params['mode']=='empty':\n  return []\n rows=nodes(entity_type='原子事实',limit=1)\n return traverse(rows[0]['node_id'],'归属于')",
            {"type": "object", "properties": {"mode": {"type": "string"}}, "required": ["mode"]},
            {"type": "array"},
            ["schema"],
            trial_inputs=({"mode": "empty"}, {"mode": "actual"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root, f)
            report = self.admit(bundle, self.graph(), root / "admission.json")
            self.assertEqual(report["verdict"], "passed")
            trials = [s for s in report["scenarios"] if s["scenario_id"] == "high_degree_out"]
            self.assertTrue(any(s["status"] == "passed" and s["required"] for s in trials))
            self.assertTrue(
                any(s["error_type"] == "CoverageGap" and not s["required"] for s in trials)
            )

    def test_internal_seed_resource_failure_remains_rejected(self):
        g = gvtest_graph()
        nodes = list(g.nodes)
        for i in range(80):
            nid = f"heavy-{i}"
            g.add_node(nid, etype="人物", __key__="{}", __sources__=["conv-x"], 姓名=str(i))
            g.add_edge(nodes[1], nid, relation="归属于")
        graph = GraphResult(g, {b.source.id: b for b in corpus()})
        f = Asset(
            "f_internal",
            "F",
            "def run(params):\n seeds=nodes(entity_type='原子事实',limit=1)\n rows=traverse(seeds[0]['node_id'],'归属于')\n out=[]\n for row in rows:\n  out.append(row)\n return out",
            {"type": "object", "properties": {"mode": {"type": "string"}}},
            {"type": "array"},
            ["schema"],
            trial_inputs=({"mode": "all"},),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root, f)
            with self.assertRaises(AdmissionError) as caught:
                self.admit(bundle, graph, root / "admission.json", RunConfig(function_steps=100))
            report = caught.exception.report
            self.assertTrue(
                any(
                    s["scenario_id"] == "high_degree_out"
                    and s["status"] == "failed"
                    and s["error_type"] == "SandboxError"
                    for s in report["scenarios"]
                )
            )
