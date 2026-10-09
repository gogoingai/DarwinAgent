import asyncio
import json
import tempfile
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest import mock

import networkx as nx

from darwinagent.agents import ExtractionAgent
from darwinagent.config import RunConfig
from darwinagent.kernel.assets import KernelAssets
from darwinagent.kernel.counterexamples import run_probes
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kernel.validation import validate_graph
from darwinagent.kg.graph import node_id
from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import Interpreter, Limits, SandboxError, admit
from tests.support.device import case, client, spec


class RuntimeGuardTests(unittest.TestCase):
    def setup_graph(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        c = case()
        s = spec(root / "assets")
        config = RunConfig()
        runtime = KernelRuntime(s.bundle, config)
        graph = asyncio.run(
            ExtractionAgent(runtime, client(c), config, "test").extract_entities(c.corpus)
        )
        return root, c, s, runtime, graph

    def test_wrong_function_result_type(self):
        root, c, s, runtime, graph = self.setup_graph()
        assets = [
            replace(a, output_contract={"type": "number"}) if a.kind == "F" else a
            for a in s.bundle.assets.assets
        ]
        rt = KernelRuntime(KernelAssets(tuple(assets)).export(root / "wrong"), RunConfig())
        with self.assertRaises(ValueError):
            rt.call("device_lookup", {"serial": "D-17"}, graph)

    def test_check_cannot_mutate_snapshot(self):
        source = 'def check(candidate):\n items = candidate["evidence"]\n items.append({})\n return {"ok":True,"issues":[]}'
        with self.assertRaises(SandboxError):
            Interpreter(admit(source, "C"), {}).execute({"evidence": [{}]})

    def test_check_must_not_return_candidate(self):
        root, c, s, runtime, graph = self.setup_graph()
        assets = [
            replace(
                a,
                content='def check(candidate):\n return {"ok":True,"issues":[],"answer":"override"}',
            )
            if a.kind == "C"
            else a
            for a in s.bundle.assets.assets
        ]
        rt = KernelRuntime(KernelAssets(tuple(assets)).export(root / "wrong"), RunConfig())
        with self.assertRaises(ValueError):
            rt.checks.run("answer", {"status": "abstained"})

    def test_actual_graph_type_contract_and_source_checks(self):
        root, c, s, runtime, graph = self.setup_graph()
        nd = next(iter(graph.graph.nodes.values()))
        for key, value in [
            ("__key__", '{"serial":"D-17","date":"invalid-date"}'),
            ("__sources__", ["forged"]),
        ]:
            old = nd[key]
            nd[key] = value
            with self.assertRaises(ValueError):
                validate_graph(graph, runtime.schema)
            nd[key] = old

    def test_behavior_probe_tolerates_partial_dates(self):
        # 冻结记忆含年/年月粒度日期（'2022'）：时间平移探针须跳过该值而非崩溃。
        # 回归：R2 提案轮图阶段 199 题全灭于 fromisoformat('2022')（2026-10-04）。
        root, c, s, runtime, graph = self.setup_graph()
        nd = next(iter(graph.graph.nodes.values()))
        nd["date"] = "2022"
        records = run_probes(runtime, graph)
        self.assertTrue(all(r["status"] == "passed" for r in records))

    def test_behavior_probe_rejects_subject_lookup_constant(self):
        root, c, s, runtime, graph = self.setup_graph()
        assets = [
            replace(
                a,
                content="def run(params):\n return nodes('Maintenance', {'serial':'D-17'}, limit=20)",
            )
            if a.kind == "F"
            else a
            for a in s.bundle.assets.assets
        ]
        rt = KernelRuntime(KernelAssets(tuple(assets)).export(root / "constant"), RunConfig())
        with self.assertRaises(ValueError):
            run_probes(rt, graph)

    def test_behavior_probe_compares_nested_rows_as_multisets(self):
        _, _, _, runtime, graph = self.setup_graph()
        graph = replace(graph, graph=nx.MultiDiGraph(graph.graph))
        first = deepcopy(next(iter(graph.graph.nodes.values())))
        key = {**json.loads(first["__key__"]), "serial": "Q-98"}
        first["__key__"] = json.dumps(key)
        first["__claims__"] = []
        graph.graph.add_node(node_id("Maintenance", key), **first)

        def call(_asset, _params, current):
            rows = list(DataCapabilities(current).rows.values())
            if rows[0]["serial"].startswith("cf_"):
                rows.reverse()
            return {"data": {"page": {"rows": rows}, "truncated": False}}

        with mock.patch.object(runtime, "call", side_effect=call):
            records = run_probes(runtime, graph)
        self.assertTrue(all(r["status"] == "passed" for r in records))

        def lost_row(asset, params, current):
            result = call(asset, params, current)
            if result["data"]["page"]["rows"][0]["serial"].startswith("cf_"):
                result["data"]["page"]["rows"].pop()
            return result

        with mock.patch.object(runtime, "call", side_effect=lost_row):
            with self.assertRaisesRegex(ValueError, "F counterexample failed"):
                run_probes(runtime, graph)

    def test_timeout_bound_and_native_output_bound(self):
        fn = admit("def run(params):\n return [x for x in range(10000)]")
        start = time.monotonic()
        with self.assertRaises(SandboxError):
            Interpreter(fn, {}, Limits(1000000, 0.0001, 500000)).execute({})
        self.assertLess(time.monotonic() - start, 1)
        fn = admit('def run(params):\n return params["x"].replace("", params["x"])')
        with self.assertRaises(SandboxError):
            Interpreter(fn, {}, Limits(result_bytes=1000)).execute({"x": "a" * 900})

    def test_prompt_does_not_expand_visible_inputs(self):
        root, c, s, runtime, graph = self.setup_graph()
        from darwinagent.kernel.assets import Asset

        with self.assertRaises(ValueError):
            Asset("prompt", "P", "${evaluator}", role="answer")
