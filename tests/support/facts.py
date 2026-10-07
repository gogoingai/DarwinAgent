"""Offline shared facts fixtures; no test-case dependencies."""

from pathlib import Path

from darwinagent.contracts import (
    AtomicFact,
    CorpusBlock,
    EntityRef,
    FactEvidence,
    FactTime,
    SourceRef,
)
from darwinagent.schema.model import Schema

SEED = Path(__file__).resolve().parents[2] / "tasks/conversation_memory/assets/S/schema.yaml"


def block(sid, text, speaker="甲", date="2024-05-01"):
    return CorpusBlock(
        SourceRef("message_text", "conv-t", sid), text, {"speaker": speaker, "date": date}
    )


def sample_facts():
    b1 = block("1", "我下周修打印机。")
    b2 = block("2", "乙没有去过巴黎。", speaker="乙", date="2024-05-08")

    def ev(b, quote):
        start = b.text.find(quote)
        return FactEvidence(b.source.id, quote, start, start + len(quote))

    f1 = AtomicFact.create(
        text="甲计划下周维修打印机",
        subject=EntityRef("person", "甲"),
        predicate="维修",
        object_entity=EntityRef("object", "打印机"),
        modality="plan",
        time=FactTime("下周", "day", anchor_source_id=b1.source.id),
        evidence=(ev(b1, "我下周修打印机"),),
    )
    f2 = AtomicFact.create(
        text="乙没有去过巴黎",
        subject=EntityRef("person", "乙"),
        predicate="去过",
        object_entity=EntityRef("place", "巴黎"),
        polarity="negative",
        time=FactTime("未注明", "unknown"),
        evidence=(ev(b2, "乙没有去过巴黎"),),
    )
    return b1, b2, f1, f2


class FakeRuntime:
    def __init__(self, schema):
        self.schema = schema

    def prompt(self, role):
        return "按任务指引抽取。"


def fact_reply(sid, quote, **overrides):
    fact = {
        "text": "甲计划下周维修打印机",
        "subject": {"class": "person", "name": "甲"},
        "predicate": "维修",
        "object": {"entity": {"class": "object", "name": "打印机"}},
        "polarity": "positive",
        "modality": "plan",
        "time": {"raw": "下周", "precision": "day", "start": "", "end": "", "relative": True},
        "evidence": [{"source_id": sid, "quote": quote}],
    }
    fact.update(overrides)
    return {"facts": [fact]}


def travel_view_schema():
    yaml = SEED.read_text()
    yaml = yaml.replace(
        "entity_classes: [person, object, place, organization, activity, topic]",
        "entity_classes: [city]\n  materialized:\n    City: {entity_class: city}",
    )
    yaml = yaml.replace(
        "relation_types:",
        """  City:
    description: 类型化物化视图——从事实谓词重建的表行
    primary_key: [city]
    attributes:
      - {name: city, dtype: string}
      - {name: state, dtype: string}
relation_types:""",
    )
    yaml = yaml.replace(
        "axioms: []",
        """  materialized_from: {domain: [City], range: AtomicFact, description: 视图行由哪些事实物化}
axioms: []""",
    )
    return Schema.from_yaml(yaml)
