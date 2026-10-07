"""Offline regression scenarios for graph integrity."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    AtomicFact,
    EntityRef,
    FactEvidence,
    FactTime,
    FactValue,
    MemoryResult,
)
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.counterexamples import run_probes
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kg.assembler import GraphAssembler, anchoring_invariants
from darwinagent.kg.graph import node_id
from darwinagent.runtime.artifacts import digest
from darwinagent.schema.model import Schema
from tests.support.facts import SEED, block, travel_view_schema


class GraphMemoryConsistency(unittest.TestCase):
    def schema(self):
        return Schema.from_yaml(SEED.read_text())

    def test_visible_attributes_must_match_fact_definition(self):
        b1 = block("1", "我下周修打印机。")
        start = b1.text.find("我下周修打印机")
        fact = AtomicFact.create(
            text="甲计划下周维修打印机",
            subject=EntityRef("person", "甲"),
            predicate="维修",
            modality="plan",
            time=FactTime("下周", "day", anchor_source_id=b1.source.id),
            evidence=(FactEvidence(b1.source.id, "我下周修打印机", start, start + 7),),
        )
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        graph = GraphAssembler.build(memory, None, self.schema())
        nid = node_id("AtomicFact", {"id": fact.id})
        graph.graph.nodes[nid]["text"] = "被篡改的可检索命题"
        self.assertTrue(
            any("不一致" in e for e in anchoring_invariants(graph.graph, memory.corpus))
        )


class MaterializationSemantics(unittest.TestCase):
    def build(self, *facts):
        b1 = block("1", "Houston is a city in Texas.")
        schema = travel_view_schema()
        memory = MemoryResult({b1.source.id: b1}, facts)
        return GraphAssembler.build(memory, None, schema), schema

    def fact(self, predicate, value, polarity="positive", modality="statement"):
        b1 = block("1", "x")
        return AtomicFact.create(
            text=f"Houston {predicate} {value}",
            subject=EntityRef("city", "Houston"),
            predicate=predicate,
            object_value=FactValue("string", value),
            polarity=polarity,
            modality=modality,
            time=FactTime("未注明"),
            evidence=(FactEvidence(b1.source.id, "Houston", 0, 7),),
        )

    def test_negative_fact_does_not_materialize(self):
        graph, _ = self.build(
            self.fact("city", "Houston"),
            self.fact("state", "Texas"),
            self.fact("state", "NotTexas", polarity="negative"),
        )
        rows = [
            (nd.get("state")) for _, nd in graph.graph.nodes(data=True) if nd.get("etype") == "City"
        ]
        self.assertEqual(rows, ["Texas"])

    def test_conflicting_values_are_not_silently_picked(self):
        graph, _ = self.build(
            self.fact("city", "Houston"),
            self.fact("state", "Texas"),
            self.fact("state", "California"),
        )
        rows = [
            (nd.get("state")) for _, nd in graph.graph.nodes(data=True) if nd.get("etype") == "City"
        ]
        self.assertEqual(rows, [None])
        self.assertGreaterEqual(graph.diagnostics[0]["skipped_view_materializations"], 1)


class ProbeRenameScope(unittest.TestCase):
    def runtime_with(self, names):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        if True:
            root = Path(td.name)
            assets = [
                Asset(
                    "schema",
                    "S",
                    SEED.read_text(),
                    {"type": "any"},
                    {"type": "any"},
                    (),
                    description="seed",
                ),
                Asset(
                    "f1",
                    "F",
                    "def run(params):\n rows = nodes('Entity', {'name': params['name']}, limit=5)\n return [r.get('text','') for r in rows]",
                    {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                    {"type": "array"},
                    ["schema"],
                    description="f",
                    trial_inputs=({"name": names[0]},),
                ),
                Asset(
                    "c1",
                    "C",
                    'def check(candidate):\n return {"ok": True, "issues": []}',
                    stage="graph",
                    schema_dependencies=["schema"],
                    description="c",
                ),
                *[
                    Asset(f"p_{r}", "P", t, role=r, schema_dependencies=["schema"], description=r)
                    for r, t in [
                        ("extract", "e ${schema}"),
                        ("tools", "t ${schema} ${tools}"),
                        ("answer", "a ${schema}"),
                        ("review", "r ${schema}"),
                    ]
                ],
            ]
            bundle = KernelAssets(tuple(assets), {"kind": "test"}).export(root / "assets")
            return KernelRuntime(bundle, RunConfig())

    def test_numeric_and_hexish_names_do_not_corrupt_probes(self):
        runtime = self.runtime_with(["卡罗琳"])
        b1 = block("1", "17 与 abc123 与 卡罗琳 都在。")

        def mk(name):
            return AtomicFact.create(
                text=f"{name} 存在",
                subject=EntityRef("person", name),
                predicate="存在",
                time=FactTime("未注明"),
                evidence=(
                    FactEvidence(
                        b1.source.id,
                        name if name in b1.text else "卡罗琳",
                        b1.text.find(name) if name in b1.text else 0,
                        (b1.text.find(name) + len(name)) if name in b1.text else 3,
                    ),
                ),
            )

        memory = MemoryResult({b1.source.id: b1}, (mk("17"), mk("abc123"), mk("卡罗琳")))
        graph = GraphAssembler.build(memory, None, runtime.schema)
        records = run_probes(runtime, graph, memory)  # must not raise
        self.assertTrue(all(r.get("status") == "passed" for r in records if r.get("probe")))


class StructuralEdgeIntegrity(unittest.TestCase):
    def build_two_facts(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1 = block("1", "甲修打印机。乙去了巴黎。")

        def mk(text, name, cls, quote):
            start = b1.text.find(quote)
            return AtomicFact.create(
                text=text,
                subject=EntityRef(cls, name),
                predicate="行动",
                time=FactTime("未注明"),
                evidence=(FactEvidence(b1.source.id, quote, start, start + len(quote)),),
            )

        f1 = mk("甲修打印机", "甲", "person", "甲修打印机")
        f2 = mk("乙去了巴黎", "乙", "person", "乙去了巴黎")
        memory = MemoryResult({b1.source.id: b1}, (f1, f2))
        graph = GraphAssembler.build(memory, None, schema)
        return graph, memory, f1, f2

    def test_swapped_subject_edges_rejected(self):
        import networkx as nx

        graph, memory, f1, f2 = self.build_two_facts()
        broken = nx.MultiDiGraph(graph.graph)
        n1 = node_id("AtomicFact", {"id": f1.id})
        n2 = node_id("AtomicFact", {"id": f2.id})
        e1 = [e for e in broken.out_edges(n1, keys=True) if e[2] == "subject"][0]
        e2 = [e for e in broken.out_edges(n2, keys=True) if e[2] == "subject"][0]
        broken.remove_edge(n1, e1[1], key="subject")
        broken.remove_edge(n2, e2[1], key="subject")
        broken.add_edge(n1, e2[1], key="subject", relation="subject")
        broken.add_edge(n2, e1[1], key="subject", relation="subject")
        self.assertTrue(
            any("subject 连边" in e for e in anchoring_invariants(broken, memory.corpus))
        )

    def test_rewritten_time_node_rejected(self):
        import networkx as nx

        graph, memory, f1, f2 = self.build_two_facts()
        broken = nx.MultiDiGraph(graph.graph)
        t = node_id("Time", {"id": digest(f1.time.to_dict())})
        broken.nodes[t]["raw"] = "被改写的时间"
        self.assertTrue(
            any(
                "时间节点" in e and "不一致" in e
                for e in anchoring_invariants(broken, memory.corpus)
            )
        )

    def test_moved_evidence_edge_rejected(self):
        import networkx as nx

        graph, memory, f1, f2 = self.build_two_facts()
        broken = nx.MultiDiGraph(graph.graph)
        n1 = node_id("AtomicFact", {"id": f1.id})
        _n2 = node_id("AtomicFact", {"id": f2.id})
        s2 = node_id(
            "EvidenceSpan",
            {
                "id": digest(
                    {
                        "source_id": f2.evidence[0].source_id,
                        "quote": f2.evidence[0].quote,
                        "start": f2.evidence[0].start,
                        "end": f2.evidence[0].end,
                    }
                )
            },
        )
        broken.add_edge(n1, s2, key="evidence", relation="evidence")
        self.assertTrue(
            any("evidence 连边" in e for e in anchoring_invariants(broken, memory.corpus))
        )
