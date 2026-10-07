"""Unit coverage for the two-stage split: fact memory extraction and anchored assembly."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import networkx as nx

from darwinagent.agents.extraction import Segment, bisect, looks_truncated, plan_batches, ExtractionAgent
from darwinagent.config import RunConfig
from darwinagent.contracts import (AtomicFact, CorpusBlock, EntityRef, FactEvidence, FactTime, FactValue,
                           MemoryResult, SourceRef)
from darwinagent.kg.assembler import GraphAssembler, anchoring_errors, anchoring_invariants, recover_facts
from darwinagent.kernel.validation import validate_graph
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.artifacts import digest
from darwinagent.schema.model import Schema

SEED = Path(__file__).resolve().parents[2] / 'tasks/conversation_memory/assets/S/schema.yaml'


def block(sid, text, speaker='甲', date='2024-05-01'):
    return CorpusBlock(SourceRef('message_text', 'conv-t', sid), text, {'speaker': speaker, 'date': date})


class BatchingTests(unittest.TestCase):
    def test_message_boundary_and_oversized(self):
        b1 = block('a', 'x' * 50); b2 = block('b', 'y' * 60); b3 = block('c', 'z' * 40)
        big = block('d', 'w' * 500)
        batches = plan_batches([b1, b2, b3, big], 100)
        self.assertEqual([[s.block.source.location for s in batch] for batch in batches],
                         [['a'], ['b', 'c'], ['d']])

    def test_bisect_keeps_offsets(self):
        b1 = block('a', 'x' * 40); b2 = block('b', 'y' * 60)
        left, right = bisect([Segment(b1, 0, 40), Segment(b2, 0, 60)])
        self.assertEqual([s.block.source.location for s in left], ['a'])
        self.assertEqual([s.block.source.location for s in right], ['b'])
        l2, r2 = bisect([Segment(b1, 10, 90)])
        self.assertEqual((l2[0].start, l2[0].end), (10, 50))
        self.assertEqual((r2[0].start, r2[0].end), (50, 90))

    def test_truncation_detection(self):
        self.assertTrue(looks_truncated('{"facts":[{"text":"abc'))
        self.assertTrue(looks_truncated('{"facts":[1,'))
        self.assertFalse(looks_truncated('{"facts":[1]}'))
        self.assertFalse(looks_truncated(''))
        self.assertFalse(looks_truncated('{"facts": [ {"text": "半角,逗号"} ]}'))


def sample_facts():
    b1 = block('1', '我下周修打印机。')
    b2 = block('2', '乙没有去过巴黎。', speaker='乙', date='2024-05-08')

    def ev(b, quote):
        start = b.text.find(quote)
        return FactEvidence(b.source.id, quote, start, start + len(quote))
    f1 = AtomicFact.create(text='甲计划下周维修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                           object_entity=EntityRef('object', '打印机'), modality='plan',
                           time=FactTime('下周', 'day', anchor_source_id=b1.source.id),
                           evidence=(ev(b1, '我下周修打印机'),))
    f2 = AtomicFact.create(text='乙没有去过巴黎', subject=EntityRef('person', '乙'), predicate='去过',
                           object_entity=EntityRef('place', '巴黎'), polarity='negative',
                           time=FactTime('未注明', 'unknown'),
                           evidence=(ev(b2, '乙没有去过巴黎'),))
    return b1, b2, f1, f2


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_yaml(SEED.read_text())
        self.assertEqual(anchoring_errors(self.schema), [])

    def build(self, facts_blocks):
        blocks = [b for b, _ in facts_blocks]
        memory = MemoryResult({b.source.id: b for b in blocks}, tuple(f for _, f in facts_blocks))
        return memory, GraphAssembler.build(memory, None, self.schema)

    def test_anchored_structure_and_roundtrip(self):
        b1, b2, f1, f2 = sample_facts()
        memory, graph = self.build([(b1, f1), (b2, f2)])
        types = {}
        for _, nd in graph.graph.nodes(data=True):
            types[nd['etype']] = types.get(nd['etype'], 0) + 1
        self.assertEqual(types['AtomicFact'], 2)
        self.assertEqual(types['Entity'], 4)
        self.assertEqual(types['EvidenceSpan'], 2)
        self.assertEqual(types['Source'], 2)
        self.assertEqual(types['Time'], 2)
        self.assertEqual(digest(recover_facts(graph.graph)), memory.fingerprint)
        validate_graph(graph, self.schema, memory.fingerprint)

    def test_deterministic_rebuild(self):
        b1, b2, f1, f2 = sample_facts()
        memory, first = self.build([(b1, f1), (b2, f2)])
        memory2 = MemoryResult({b.source.id: b for b in (b2, b1)}, (f2, f1))
        second = GraphAssembler.build(memory2, None, self.schema)
        self.assertEqual(json.dumps(nx.node_link_data(first.graph, edges='links'), ensure_ascii=False),
                         json.dumps(nx.node_link_data(second.graph, edges='links'), ensure_ascii=False))

    def test_shared_evidence_deduplicates_and_anchors_time(self):
        b1 = block('1', '甲说：我下周修打印机，真的下周。')
        quote = '我下周修打印机'
        start = b1.text.find(quote)
        f1 = AtomicFact.create(text='甲计划下周维修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                               modality='plan', time=FactTime('下周', 'day', anchor_source_id=b1.source.id),
                               evidence=(FactEvidence(b1.source.id, quote, start, start + len(quote)),))
        f2 = AtomicFact.create(text='甲重申下周会修打印机', subject=EntityRef('person', '甲'), predicate='重申',
                               modality='statement', time=FactTime('下周', 'day', anchor_source_id=b1.source.id),
                               evidence=(FactEvidence(b1.source.id, quote, start, start + len(quote)),))
        memory, graph = self.build([(b1, f1), (b1, f2)])
        spans = [nd for _, nd in graph.graph.nodes(data=True) if nd['etype'] == 'EvidenceSpan']
        self.assertEqual(len(spans), 1)
        anchors = [1 for h, t, k in graph.graph.edges(keys=True) if k == 'time_anchor']
        self.assertEqual(len(anchors), 1)
        self.assertEqual(digest(recover_facts(graph.graph)), memory.fingerprint)

    def test_orphan_structure_rejected(self):
        b1, b2, f1, f2 = sample_facts()
        memory, graph = self.build([(b1, f1), (b2, f2)])
        broken = nx.MultiDiGraph(graph.graph)
        broken.add_node('Entity::[["class","person"],["name","路人"]]', etype='Entity',
                        __key__=json.dumps({'class': 'person', 'name': '路人'}, ensure_ascii=False),
                        __merged__=0, __sources__=[b1.source.id])
        self.assertTrue(anchoring_invariants(broken, graph.sources))

    def test_tampered_fact_node_rejected(self):
        b1, b2, f1, f2 = sample_facts()
        memory, graph = self.build([(b1, f1), (b2, f2)])
        nid = next(n for n, nd in graph.graph.nodes(data=True) if nd['etype'] == 'AtomicFact')
        payload = json.loads(graph.graph.nodes[nid]['__fact__'])
        payload['text'] = '篡改后的命题'
        graph.graph.nodes[nid]['__fact__'] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.assertRaises(ValueError):
            validate_graph(graph, self.schema, memory.fingerprint)

    def test_schema_must_keep_anchor(self):
        import dataclasses
        dropped = dataclasses.replace(self.schema,
                                      entities=[e for e in self.schema.entities if e.name != 'Entity'])
        self.assertTrue(anchoring_errors(dropped))
        no_classes = dataclasses.replace(self.schema, meta={'task': 'conversation_memory'})
        self.assertTrue(anchoring_errors(no_classes))
        altered = dataclasses.replace(self.schema, relations=[
            dataclasses.replace(r, range='Entity') if r.name == 'evidence' else r for r in self.schema.relations])
        self.assertTrue(anchoring_errors(altered))


class FakeRuntime:
    def __init__(self, schema):
        self.schema = schema

    def prompt(self, role):
        return '按任务指引抽取。'


def fact_reply(sid, quote, **overrides):
    fact = {'text': '甲计划下周维修打印机', 'subject': {'class': 'person', 'name': '甲'},
            'predicate': '维修', 'object': {'entity': {'class': 'object', 'name': '打印机'}},
            'polarity': 'positive', 'modality': 'plan',
            'time': {'raw': '下周', 'precision': 'day', 'start': '', 'end': '', 'relative': True},
            'evidence': [{'source_id': sid, 'quote': quote}]}
    fact.update(overrides)
    return {'facts': [fact]}


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_yaml(SEED.read_text())
        self.config = RunConfig()

    async def run_extract(self, corpus, replies):
        client = RecordedClient({'extraction': replies})
        agent = ExtractionAgent(FakeRuntime(self.schema), client, self.config, 'ns')
        return await agent.extract(corpus), client

    def test_valid_reply_yields_memory_with_offsets(self):
        b1 = block('1', '我下周修打印机。')
        import asyncio
        memory, client = asyncio.run(self.run_extract([b1], [fact_reply('m0', '我下周修打印机')]))
        self.assertEqual(len(memory.facts), 1)
        fact = memory.facts[0]
        self.assertEqual(fact.evidence[0].start, 0)
        self.assertEqual(fact.evidence[0].source_id, b1.source.id)
        self.assertEqual(b1.text[fact.evidence[0].start:fact.evidence[0].end], fact.evidence[0].quote)
        self.assertEqual(fact.time.anchor_source_id, b1.source.id)
        self.assertEqual(fact.object_entity.name, '打印机')
        self.assertEqual(client.calls[0]['role'], 'extraction')

    def test_pointed_feedback_then_correction(self):
        b1 = block('1', '我下周修打印机。')
        bad = fact_reply('m0', '我下周修打印机')
        bad['facts'][0]['evidence'][0]['quote'] = '我下周修打印机，'  # punctuation rewritten
        import asyncio
        memory, client = asyncio.run(self.run_extract([b1], [bad, fact_reply('m0', '我下周修打印机')]))
        self.assertEqual(len(memory.facts), 1)
        feedback = client.calls[1]['messages'][-1]['content']
        self.assertIn('facts[0].evidence[0].quote', feedback)
        self.assertIn('逐字', feedback)
        self.assertIn('m0', feedback)

    def test_unknown_class_rejected_with_allowed_list(self):
        b1 = block('1', '我下周修打印机。')
        bad = fact_reply('m0', '我下周修打印机')
        bad['facts'][0]['subject']['class'] = 'human'
        import asyncio
        with self.assertRaises(Exception):
            asyncio.run(self.run_extract([b1], [bad, bad, bad]))

    def test_final_batch_failure_raises(self):
        b1 = block('1', '我下周修打印机。')
        bad = fact_reply('m0', '不存在')
        import asyncio
        with self.assertRaises(Exception) as ctx:
            asyncio.run(self.run_extract([b1], [bad, bad, bad]))
        self.assertIn('batch', str(ctx.exception))

    def test_multi_message_batches_and_value_objects(self):
        b1 = block('1', '乙身高一米八。')
        import asyncio
        reply = {'facts': [{'text': '乙的身高是一米八', 'subject': {'class': 'person', 'name': '乙'},
                            'predicate': '身高', 'object': {'value': {'dtype': 'float', 'value': 1.8}},
                            'polarity': 'positive', 'modality': 'statement',
                            'time': {'raw': '未注明', 'precision': 'unknown', 'start': '', 'end': '', 'relative': False},
                            'evidence': [{'source_id': 'm0', 'quote': '乙身高一米八'}]}]}
        memory, _ = asyncio.run(self.run_extract([b1], [reply]))
        self.assertEqual(memory.facts[0].object_value, FactValue('float', '1.8'))


def travel_view_schema():
    yaml = SEED.read_text()
    yaml = yaml.replace('entity_classes: [person, object, place, organization, activity, topic]',
                        'entity_classes: [city]\n  materialized:\n    City: {entity_class: city}')
    yaml = yaml.replace('relation_types:', '''  City:
    description: 类型化物化视图——从事实谓词重建的表行
    primary_key: [city]
    attributes:
      - {name: city, dtype: string}
      - {name: state, dtype: string}
relation_types:''')
    yaml = yaml.replace('axioms: []', '''  materialized_from: {domain: [City], range: AtomicFact, description: 视图行由哪些事实物化}
axioms: []''')
    return Schema.from_yaml(yaml)


class MaterializedViewTests(unittest.TestCase):
    def test_view_rebuilt_from_fact_predicates_and_traceable(self):
        from darwinagent.operators.data import DataCapabilities
        schema = travel_view_schema()
        self.assertEqual(anchoring_errors(schema), [])
        b1 = block('1', 'Houston is a city in Texas.')
        def fact(predicate, value, dtype):
            return AtomicFact.create(text=f'Houston {predicate} {value}', subject=EntityRef('city', 'Houston'),
                                     predicate=predicate, object_value=FactValue(dtype, value),
                                     time=FactTime('未注明'), evidence=(FactEvidence(b1.source.id, 'Houston', 0, 7),))
        memory = MemoryResult({b1.source.id: b1}, (fact('city', 'Houston', 'string'), fact('state', 'Texas', 'string')))
        graph = GraphAssembler.build(memory, None, schema)
        rows = [r for r in DataCapabilities(graph).rows.values() if r['entity_type'] == 'City']
        self.assertEqual(rows, [{'node_id': rows[0]['node_id'], 'entity_type': 'City',
                                 'city': 'Houston', 'state': 'Texas',
                                 'source_ids': rows[0]['source_ids'], 'claims': []}])
        view_edges = [1 for h, t, k in graph.graph.edges(keys=True) if k == 'materialized_from']
        self.assertEqual(len(view_edges), 2)
        validate_graph(graph, schema, memory.fingerprint)

    def test_entity_missing_key_predicate_skips_view(self):
        schema = travel_view_schema()
        b1 = block('1', 'Nowhere has no city column.')
        f1 = AtomicFact.create(text='Nowhere state Unknown', subject=EntityRef('city', 'Nowhere'),
                               predicate='state', object_value=FactValue('string', 'Unknown'),
                               time=FactTime('未注明'), evidence=(FactEvidence(b1.source.id, 'Nowhere', 0, 7),))
        memory = MemoryResult({b1.source.id: b1}, (f1,))
        graph = GraphAssembler.build(memory, None, schema)
        self.assertFalse([nd for _, nd in graph.graph.nodes(data=True) if nd['etype'] == 'City'])
        self.assertEqual(graph.diagnostics[0]['skipped_view_materializations'], 1)


    def test_anchoring_is_opt_in_per_task(self):
        # 事实锚定是任务级声明（S meta.anchoring）：device 未声明，不携带核心词表也合法；
        # 一旦声明（如 locomo 种子），缺词表即被 validate_bundle 拒绝。
        device = Schema.from_yaml((SEED.parents[3] / 'device_maintenance/assets/S/schema.yaml').read_text())
        self.assertFalse(device.meta.get('anchoring'))
        self.assertTrue(anchoring_errors(device))
        declared = Schema.from_yaml(SEED.read_text().replace('axioms: []', 'axioms: []'))
        self.assertTrue(declared.meta.get('anchoring'))
        self.assertEqual(anchoring_errors(declared), [])


if __name__ == '__main__':
    unittest.main()
