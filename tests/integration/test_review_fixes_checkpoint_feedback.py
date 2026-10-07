"""Offline regression scenarios for checkpoint feedback."""

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
from darwinagent.runtime.artifacts import digest
from darwinagent.schema.model import Schema
from tests.support.facts import SEED, block, travel_view_schema


class CheckpointFeedbackTests(unittest.TestCase):
    """Edge-label integrity, source metadata, anchor absence and view re-derivation."""

    def seed_graph(self):
        schema = Schema.from_yaml(SEED.read_text())
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
        import networkx as nx

        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        return broken, memory, schema, fact

    def test_edge_label_mismatch_rejected(self):
        broken, memory, schema, fact = self.seed_graph()

        nid = node_id("AtomicFact", {"id": fact.id})
        for h, t, key in list(broken.out_edges(nid, keys=True)):
            if key == "subject":
                broken.remove_edge(h, t, key="subject")
                broken.add_edge(h, t, key="subject", relation="object_entity")
        errs = anchoring_invariants(broken, memory.corpus, None, schema)
        self.assertTrue(any("边标签不一致" in e for e in errs))

    def test_source_date_tamper_rejected(self):
        broken, memory, schema, fact = self.seed_graph()
        for nid, nd in broken.nodes(data=True):
            if nd.get("etype") == "Source":
                broken.nodes[nid]["date"] = "2099-01-01"
        errs = anchoring_invariants(broken, memory.corpus, None, schema)
        self.assertTrue(any("说话人/日期与语料元数据不一致" in e for e in errs))

    def test_spurious_anchor_on_anchorless_time_rejected(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1 = block("1", "甲修打印机。")
        start = b1.text.find("甲修打印机")
        fact = AtomicFact.create(
            text="甲修打印机",
            subject=EntityRef("person", "甲"),
            predicate="维修",
            modality="statement",
            time=FactTime("未注明"),
            evidence=(FactEvidence(b1.source.id, "甲修打印机", start, start + 5),),
        )
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        import networkx as nx

        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        time_nid = node_id("Time", {"id": digest(fact.time.to_dict())})
        src_nid = node_id(
            "Source", {"kind": "message_text", "document_id": "conv-t", "location": "1"}
        )
        broken.add_edge(time_nid, src_nid, key="time_anchor", relation="time_anchor")
        errs = anchoring_invariants(broken, memory.corpus, None, schema)
        self.assertTrue(any("锚点连边与事实定义不一致" in e for e in errs))

    def test_view_attribute_tamper_rejected(self):
        schema = travel_view_schema()
        b1 = block("1", "Houston is a city in Texas.")

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
                broken.nodes[nid]["state"] = "California"
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any("state 与事实推导不一致" in e for e in errs))
