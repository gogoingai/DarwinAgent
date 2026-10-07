"""Offline regression scenarios for checkpoint resume."""

import unittest

from darwinagent.contracts import (
    AtomicFact,
    EntityRef,
    FactEvidence,
    FactTime,
    FactValue,
    MemoryResult,
)
from darwinagent.kg.assembler import GraphAssembler, anchoring_invariants
from darwinagent.kg.graph import node_id
from darwinagent.schema.model import Schema
from tests.support.facts import SEED, block, travel_view_schema


class ResumeIntegrityTests(unittest.TestCase):
    """View attribute-set integrity and evidence-grounded source attribution."""

    def seed_two_blocks(self):
        b1 = block("1", "我下周修打印机。")
        b2 = block("2", "别的消息。", speaker="乙", date="2024-05-08")
        return b1, b2

    def test_view_extra_attribute_rejected(self):
        schema = travel_view_schema()
        b1 = block("1", "Houston x.")

        def fv(predicate, value):
            return AtomicFact.create(
                text=f"Houston {predicate} {value}",
                subject=EntityRef("city", "Houston"),
                predicate=predicate,
                object_value=FactValue("string", value),
                time=FactTime("未注明"),
                evidence=(FactEvidence(b1.source.id, "Houston", 0, 7),),
            )

        conflicting = (fv("city", "Houston"), fv("state", "Texas"), fv("state", "California"))
        memory = MemoryResult({b1.source.id: b1}, conflicting)
        import networkx as nx

        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        for nid, nd in broken.nodes(data=True):
            if nd.get("etype") == "City":
                broken.nodes[nid]["state"] = "Texas"  # 补回被冲突规则排除的属性
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any("属性集与事实推导不符" in e and "state" in e for e in errs))

    def test_view_missing_planned_attribute_rejected(self):
        schema = travel_view_schema()
        b1 = block("1", "Houston x.")

        def fv(predicate, value):
            return AtomicFact.create(
                text=f"Houston {predicate} {value}",
                subject=EntityRef("city", "Houston"),
                predicate=predicate,
                object_value=FactValue("string", value),
                time=FactTime("未注明"),
                evidence=(FactEvidence(b1.source.id, "Houston", 0, 7),),
            )

        memory = MemoryResult({b1.source.id: b1}, (fv("city", "Houston"), fv("state", "Texas")))
        import networkx as nx

        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        for nid, nd in broken.nodes(data=True):
            if nd.get("etype") == "City":
                del broken.nodes[nid]["state"]
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any("缺失" in e and "state" in e for e in errs))

    def test_fact_sources_swapped_to_other_registered_source_rejected(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1, b2 = self.seed_two_blocks()
        fact = AtomicFact.create(
            text="甲修打印机",
            subject=EntityRef("person", "甲"),
            predicate="维修",
            modality="statement",
            time=FactTime("未注明"),
            evidence=(FactEvidence(b1.source.id, "我下周修打印机", 0, 7),),
        )
        memory = MemoryResult({b1.source.id: b1, b2.source.id: b2}, (fact,))
        import networkx as nx

        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        nid = node_id("AtomicFact", {"id": fact.id})
        broken.nodes[nid]["__sources__"] = [b2.source.id]  # 换成另一个已登记来源
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any("来源追踪与结构不符" in e for e in errs))

    def test_entity_sources_missing_or_extra_rejected(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1, b2 = self.seed_two_blocks()
        fact = AtomicFact.create(
            text="甲修打印机",
            subject=EntityRef("person", "甲"),
            predicate="维修",
            modality="statement",
            time=FactTime("未注明"),
            evidence=(
                FactEvidence(b1.source.id, "我下周修打印机", 0, 7),
                FactEvidence(b2.source.id, "别的消息", 0, 4),
            ),
        )
        memory = MemoryResult({b1.source.id: b1, b2.source.id: b2}, (fact,))
        import networkx as nx

        graph = GraphAssembler.build(memory, None, schema)
        ent = node_id("Entity", {"class": "person", "name": "甲"})
        broken = nx.MultiDiGraph(graph.graph)
        broken.nodes[ent]["__sources__"] = [b1.source.id]  # 遗漏真实来源 b2
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any("来源追踪与结构不符" in e for e in errs))
        broken.nodes[ent]["__sources__"] = [b1.source.id, b2.source.id, "unfounded"]
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any("来源追踪与结构不符" in e for e in errs))

    def test_tool_source_ids_match_fact_evidence_on_good_graph(self):
        from darwinagent.operators.data import DataCapabilities

        schema = Schema.from_yaml(SEED.read_text())
        b1, _ = self.seed_two_blocks()
        fact = AtomicFact.create(
            text="甲修打印机",
            subject=EntityRef("person", "甲"),
            predicate="维修",
            modality="statement",
            time=FactTime("未注明"),
            evidence=(FactEvidence(b1.source.id, "我下周修打印机", 0, 7),),
        )
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        graph = GraphAssembler.build(memory, None, schema)
        rows = DataCapabilities(graph).rows
        fact_rows = [r for r in rows.values() if r["entity_type"] == "AtomicFact"]
        self.assertEqual(len(fact_rows), 1)
        self.assertEqual(set(fact_rows[0]["source_ids"]), {ev.source_id for ev in fact.evidence})
