"""Regression coverage for the 2026-10-03 review fixes (P1/P2) and the probe rename scope."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oak.config import RunConfig
from oak.contracts import AtomicFact, CorpusBlock, EntityRef, FactEvidence, FactTime, FactValue, MemoryResult, SourceRef
from oak.kg.assembler import GraphAssembler, anchoring_invariants
from oak.kg.graph import node_id
from oak.kernel.assets import Asset, KernelAssets
from oak.kernel.counterexamples import run_probes
from oak.kernel.execution import KernelRuntime
from oak.kernel.revision import AssetPatch, AssetRevisionService
from oak.operators.sandbox import Interpreter, admit
from oak.runtime.artifacts import digest
from oak.schema.model import Schema

from tests.unit.test_fact_memory import SEED, block, travel_view_schema


class InterpreterFixes(unittest.TestCase):
    def test_isinstance_resolves_type_names(self):
        fn = admit('def check(candidate):\n v = candidate.get("k", "")\n return {"ok": isinstance(v, str), "issues": []}', 'C')
        self.assertTrue(Interpreter(fn, {}).execute({'k': 'v'}).get('ok'))

    def test_unknown_name_still_rejected(self):
        with self.assertRaises(Exception):
            Interpreter(admit('def run(params):\n return undefined_name', 'F'), {}).execute({})


class AnchoredPipelineChecksTaskC(unittest.TestCase):
    """Fresh assembly must run the unified validation including task graph C (P1-2)."""

    def bundle(self, root, graph_check_ok=True):
        check = ('def check(candidate):\n return {"ok": True, "issues": []}' if graph_check_ok
                 else 'def check(candidate):\n return {"ok": False, "issues": ["总是拒绝"]}')
        assets = [
            Asset('schema', 'S', SEED.read_text(), {"type": "any"}, {"type": "any"}, (), description='seed'),
            Asset('f_find', 'F', "def run(params):\n rows = nodes('AtomicFact', {'predicate': params.get('predicate','')}, limit=5)\n return [r.get('text','') for r in rows]",
                  {"type": "object", "properties": {"predicate": {"type": "string"}}}, {"type": "array"}, ['schema'],
                  description='find facts', trial_inputs=({'predicate': '维修'},)),
            Asset('c_graph', 'C', check, stage='graph', schema_dependencies=['schema'], description='graph check'),
            Asset('c_answer', 'C', 'def check(candidate):\n return {"ok": True, "issues": []}', stage='answer',
                  schema_dependencies=['schema'], description='answer check'),
            *[Asset(f'p_{role}', 'P', text, role=role, schema_dependencies=['schema'], description=f'{role} prompt')
              for role, text in [('extract', '抽取指引 ${schema}'), ('tools', '工具指引 ${schema} ${tools}'),
                                 ('answer', '作答指引 ${schema}'), ('review', '审查指引 ${schema}')]],
        ]
        return KernelAssets(tuple(assets), {'kind': 'test'}).export(root / 'assets')

    def case(self):
        b1 = block('1', '我下周修打印机。')
        return b1

    def replies(self, b1):
        from tests.fixtures import review
        return {'extraction': [{'facts': [{
            'text': '甲计划下周维修打印机', 'subject': {'class': 'person', 'name': '甲'}, 'predicate': '维修',
            'object': {'entity': {'class': 'object', 'name': '打印机'}}, 'polarity': 'positive', 'modality': 'plan',
            'time': {'raw': '下周', 'precision': 'day', 'start': '', 'end': '', 'relative': True},
            'evidence': [{'source_id': 'm0', 'quote': '我下周修打印机'}]}]}],
            'tools': [{'action': 'call', 'asset_id': 'f_find', 'parameters': {'predicate': '维修'}},
                      {'action': 'ready'}],
            'answer': [{'status': 'answered', 'answer': '甲计划下周修打印机。', 'node_ids': ['n000000']}],
            'review': [review()]}

    def run_pipeline(self, graph_check_ok):
        from oak.engine import Pipeline
        from oak.kernel import TaskSpec
        from oak.llm.recorded import RecordedClient
        from oak.contracts import CaseInput, QuestionInput
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        bundle = self.bundle(root, graph_check_ok)
        spec = TaskSpec.load(SEED.parents[2] / 'task.yaml', bundle).with_bundle(bundle)
        b1 = self.case()
        case = CaseInput('c1', (b1,), (QuestionInput('q1', '甲计划做什么？'),))
        transport = RecordedClient(self.replies(b1))
        result = asyncio.run(Pipeline(transport, root / 'generation').run(case, spec, RunConfig(protocol_attempts=1)))
        return root / 'generation' / 'c1', result

    def test_passing_check_answers(self):
        _, result = self.run_pipeline(True)
        self.assertEqual(result.answers[0].status, 'answered')
        self.assertEqual(result.memory_count, 1)

    def test_rejecting_graph_check_fails_fresh_run_too(self):
        case_dir, result = self.run_pipeline(False)
        self.assertEqual(result.answers[0].status, 'execution_error')
        self.assertIn('总是拒绝', result.answers[0].error)
        self.assertTrue((case_dir / 'memory.complete.json').exists())


class AnchoringModeFrozen(unittest.TestCase):
    def test_s_patch_cannot_flip_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            assets = [
                Asset('schema', 'S', SEED.read_text(), {"type": "any"}, {"type": "any"}, (), description='seed'),
                Asset('f1', 'F', "def run(params):\n return []", {"type": "object"}, {"type": "array"}, ['schema'],
                      description='f', trial_inputs=({},)),
                Asset('c1', 'C', 'def check(candidate):\n return {"ok": True, "issues": []}', stage='graph',
                      schema_dependencies=['schema'], description='c'),
                *[Asset(f'p_{r}', 'P', t, role=r, schema_dependencies=['schema'], description=r)
                  for r, t in [('extract', 'e ${schema}'), ('tools', 't ${schema} ${tools}'),
                               ('answer', 'a ${schema}'), ('review', 'r ${schema}')]],
            ]
            base = KernelAssets(tuple(assets), {'kind': 'test'}).export(root / 'base')
            s = base.get('schema')
            dropped = replace(s, content=s.content.replace('  anchoring: fact-centric-v1\n', ''))
            with self.assertRaises(ValueError) as caught:
                AssetRevisionService().propose(base, [AssetPatch(dropped, s.fingerprint, '去掉锚定', ('q1',))],
                                               root / 'candidate', ['q1'])
            self.assertIn('冻结', str(caught.exception))


class GraphMemoryConsistency(unittest.TestCase):
    def schema(self):
        return Schema.from_yaml(SEED.read_text())

    def test_visible_attributes_must_match_fact_definition(self):
        b1 = block('1', '我下周修打印机。')
        start = b1.text.find('我下周修打印机')
        fact = AtomicFact.create(text='甲计划下周维修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                                 modality='plan', time=FactTime('下周', 'day', anchor_source_id=b1.source.id),
                                 evidence=(FactEvidence(b1.source.id, '我下周修打印机', start, start + 7),))
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        graph = GraphAssembler.build(memory, None, self.schema())
        nid = node_id('AtomicFact', {'id': fact.id})
        graph.graph.nodes[nid]['text'] = '被篡改的可检索命题'
        self.assertTrue(any('不一致' in e for e in anchoring_invariants(graph.graph, memory.corpus)))


class MaterializationSemantics(unittest.TestCase):
    def build(self, *facts):
        b1 = block('1', 'Houston is a city in Texas.')
        schema = travel_view_schema()
        memory = MemoryResult({b1.source.id: b1}, facts)
        return GraphAssembler.build(memory, None, schema), schema

    def fact(self, predicate, value, polarity='positive', modality='statement'):
        b1 = block('1', 'x')
        return AtomicFact.create(text=f'Houston {predicate} {value}', subject=EntityRef('city', 'Houston'),
                                 predicate=predicate, object_value=FactValue('string', value),
                                 polarity=polarity, modality=modality, time=FactTime('未注明'),
                                 evidence=(FactEvidence(b1.source.id, 'Houston', 0, 7),))

    def test_negative_fact_does_not_materialize(self):
        graph, _ = self.build(self.fact('city', 'Houston'), self.fact('state', 'Texas'),
                              self.fact('state', 'NotTexas', polarity='negative'))
        rows = [(nd.get('state')) for _, nd in graph.graph.nodes(data=True) if nd.get('etype') == 'City']
        self.assertEqual(rows, ['Texas'])

    def test_conflicting_values_are_not_silently_picked(self):
        graph, _ = self.build(self.fact('city', 'Houston'),
                              self.fact('state', 'Texas'), self.fact('state', 'California'))
        rows = [(nd.get('state')) for _, nd in graph.graph.nodes(data=True) if nd.get('etype') == 'City']
        self.assertEqual(rows, [None])
        self.assertGreaterEqual(graph.diagnostics[0]['skipped_view_materializations'], 1)


class ProbeRenameScope(unittest.TestCase):
    def runtime_with(self, names):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        if True:
            root = Path(td.name)
            assets = [
                Asset('schema', 'S', SEED.read_text(), {"type": "any"}, {"type": "any"}, (), description='seed'),
                Asset('f1', 'F', "def run(params):\n rows = nodes('Entity', {'name': params['name']}, limit=5)\n return [r.get('text','') for r in rows]",
                      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                      {"type": "array"}, ['schema'], description='f', trial_inputs=({'name': names[0]},)),
                Asset('c1', 'C', 'def check(candidate):\n return {"ok": True, "issues": []}', stage='graph',
                      schema_dependencies=['schema'], description='c'),
                *[Asset(f'p_{r}', 'P', t, role=r, schema_dependencies=['schema'], description=r)
                  for r, t in [('extract', 'e ${schema}'), ('tools', 't ${schema} ${tools}'),
                               ('answer', 'a ${schema}'), ('review', 'r ${schema}')]],
            ]
            bundle = KernelAssets(tuple(assets), {'kind': 'test'}).export(root / 'assets')
            return KernelRuntime(bundle, RunConfig())

    def test_numeric_and_hexish_names_do_not_corrupt_probes(self):
        runtime = self.runtime_with(['卡罗琳'])
        b1 = block('1', '17 与 abc123 与 卡罗琳 都在。')
        def mk(name):
            return AtomicFact.create(text=f'{name} 存在', subject=EntityRef('person', name), predicate='存在',
                                     time=FactTime('未注明'),
                                     evidence=(FactEvidence(b1.source.id, name if name in b1.text else '卡罗琳', b1.text.find(name) if name in b1.text else 0, (b1.text.find(name) + len(name)) if name in b1.text else 3),))
        memory = MemoryResult({b1.source.id: b1}, (mk('17'), mk('abc123'), mk('卡罗琳')))
        graph = GraphAssembler.build(memory, None, runtime.schema)
        records = run_probes(runtime, graph, memory)  # must not raise
        self.assertTrue(all(r.get('status') == 'passed' for r in records if r.get('probe')))


class BudgetReserveBeforeExecution(unittest.TestCase):
    def test_cap_refuses_next_round_before_it_runs(self):
        from tests.integration.test_campaign import RecordedCampaign, protocol, SERIALS
        import tests.integration.test_campaign as tc
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / 'precheck.json').write_text(json.dumps({'passed': True, 'checks': {}}))
        controller = RecordedCampaign(root, spec=protocol(rounds=1, cap=1))
        from oak.kernel import TaskSpec
        with self.assertRaises(ValueError) as caught:
            asyncio.run(controller.run(TaskSpec.load(Path(__file__).resolve().parents[2] / 'tasks/device_maintenance/task.yaml')))
        self.assertIn('cap exceeded', str(caught.exception))
        # R1 never generated: the refusal happened before execution, B0 is settled at 1.
        self.assertFalse((root / 'train' / 'R1' / 'generation').exists())
        ledger = (root / 'question_runs.jsonl').read_text()
        self.assertIn('train/B0', ledger)
        self.assertNotIn('train/R1', ledger)

    def test_reserve_is_idempotent_and_settle_does_not_recharge(self):
        from tests.integration.test_campaign import RecordedCampaign, protocol
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        controller._reserve('validation/v1', 'val-case', 190)
        controller._reserve('validation/v1', 'val-case', 190)  # cache resume
        controller._settle('validation/v1', 190)
        controller._reserve('validation/v1', 'val-case', 190)  # resume after settle
        self.assertEqual(controller._ledger_total(), 190)
        self.assertEqual(controller._ledger_rows()['validation/v1']['state'], 'settled')

    def test_decision_scan_skips_resultless_rounds(self):
        from tests.integration.test_campaign import RecordedCampaign, protocol
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        train = root / 'train'
        (train / 'B0' / 'generation' / 'train-case').mkdir(parents=True)
        (train / 'B0' / 'generation' / 'train-case' / 'result.json').write_text(json.dumps({'answers': [{}], 'asset_version': 'b0'}))
        (train / 'R1').mkdir(parents=True)  # admission failed: decision but no generation
        (train / 'R1' / 'decision.json').write_text(json.dumps({'accepted': False, 'status': 'validation_failed'}))
        (train / 'R2' / 'generation' / 'train-case').mkdir(parents=True)
        (train / 'R2' / 'generation' / 'train-case' / 'result.json').write_text(json.dumps({'answers': [{}], 'asset_version': 'r2'}))
        (train / 'R2' / 'decision.json').write_text(json.dumps({'accepted': True, 'candidate_version': 'r2'}))
        controller._settle_train_from_decisions()
        rows = controller._ledger_rows()
        self.assertIn('train/B0', rows)
        self.assertIn('train/R2', rows)
        self.assertNotIn('train/R1', rows)


class StructuralEdgeIntegrity(unittest.TestCase):
    def build_two_facts(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1 = block('1', '甲修打印机。乙去了巴黎。')
        def mk(text, name, cls, quote):
            start = b1.text.find(quote)
            return AtomicFact.create(text=text, subject=EntityRef(cls, name), predicate='行动',
                                     time=FactTime('未注明'), evidence=(FactEvidence(b1.source.id, quote, start, start + len(quote)),))
        f1 = mk('甲修打印机', '甲', 'person', '甲修打印机')
        f2 = mk('乙去了巴黎', '乙', 'person', '乙去了巴黎')
        memory = MemoryResult({b1.source.id: b1}, (f1, f2))
        graph = GraphAssembler.build(memory, None, schema)
        return graph, memory, f1, f2

    def test_swapped_subject_edges_rejected(self):
        import networkx as nx
        graph, memory, f1, f2 = self.build_two_facts()
        broken = nx.MultiDiGraph(graph.graph)
        n1 = node_id('AtomicFact', {'id': f1.id}); n2 = node_id('AtomicFact', {'id': f2.id})
        e1 = [e for e in broken.out_edges(n1, keys=True) if e[2] == 'subject'][0]
        e2 = [e for e in broken.out_edges(n2, keys=True) if e[2] == 'subject'][0]
        broken.remove_edge(n1, e1[1], key='subject'); broken.remove_edge(n2, e2[1], key='subject')
        broken.add_edge(n1, e2[1], key='subject', relation='subject')
        broken.add_edge(n2, e1[1], key='subject', relation='subject')
        self.assertTrue(any('subject 连边' in e for e in anchoring_invariants(broken, memory.corpus)))

    def test_rewritten_time_node_rejected(self):
        import networkx as nx
        graph, memory, f1, f2 = self.build_two_facts()
        broken = nx.MultiDiGraph(graph.graph)
        t = node_id('Time', {'id': digest(f1.time.to_dict())})
        broken.nodes[t]['raw'] = '被改写的时间'
        self.assertTrue(any('时间节点' in e and '不一致' in e for e in anchoring_invariants(broken, memory.corpus)))

    def test_moved_evidence_edge_rejected(self):
        import networkx as nx
        graph, memory, f1, f2 = self.build_two_facts()
        broken = nx.MultiDiGraph(graph.graph)
        n1 = node_id('AtomicFact', {'id': f1.id}); n2 = node_id('AtomicFact', {'id': f2.id})
        s2 = node_id('EvidenceSpan', {'id': digest({'source_id': f2.evidence[0].source_id, 'quote': f2.evidence[0].quote,
                                                    'start': f2.evidence[0].start, 'end': f2.evidence[0].end})})
        broken.add_edge(n1, s2, key='evidence', relation='evidence')
        self.assertTrue(any('evidence 连边' in e for e in anchoring_invariants(broken, memory.corpus)))




class ReviewRoundThree(unittest.TestCase):
    """Edge-label integrity, source metadata, anchor absence and view re-derivation."""

    def seed_graph(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1 = block('1', '我下周修打印机。')
        start = b1.text.find('我下周修打印机')
        fact = AtomicFact.create(text='甲计划下周维修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                                 modality='plan', time=FactTime('下周', 'day', anchor_source_id=b1.source.id),
                                 evidence=(FactEvidence(b1.source.id, '我下周修打印机', start, start + 7),))
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        return broken, memory, schema, fact

    def test_edge_label_mismatch_rejected(self):
        broken, memory, schema, fact = self.seed_graph()
        import networkx as nx
        nid = node_id('AtomicFact', {'id': fact.id})
        for h, t, key in list(broken.out_edges(nid, keys=True)):
            if key == 'subject':
                broken.remove_edge(h, t, key='subject')
                broken.add_edge(h, t, key='subject', relation='object_entity')
        errs = anchoring_invariants(broken, memory.corpus, None, schema)
        self.assertTrue(any('边标签不一致' in e for e in errs))

    def test_source_date_tamper_rejected(self):
        broken, memory, schema, fact = self.seed_graph()
        for nid, nd in broken.nodes(data=True):
            if nd.get('etype') == 'Source':
                broken.nodes[nid]['date'] = '2099-01-01'
        errs = anchoring_invariants(broken, memory.corpus, None, schema)
        self.assertTrue(any('说话人/日期与语料元数据不一致' in e for e in errs))

    def test_spurious_anchor_on_anchorless_time_rejected(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1 = block('1', '甲修打印机。')
        start = b1.text.find('甲修打印机')
        fact = AtomicFact.create(text='甲修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                                 modality='statement', time=FactTime('未注明'),
                                 evidence=(FactEvidence(b1.source.id, '甲修打印机', start, start + 5),))
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        time_nid = node_id('Time', {'id': digest(fact.time.to_dict())})
        src_nid = node_id('Source', {'kind': 'message_text', 'document_id': 'conv-t', 'location': '1'})
        broken.add_edge(time_nid, src_nid, key='time_anchor', relation='time_anchor')
        errs = anchoring_invariants(broken, memory.corpus, None, schema)
        self.assertTrue(any('锚点连边与事实定义不一致' in e for e in errs))

    def test_view_attribute_tamper_rejected(self):
        schema = travel_view_schema()
        b1 = block('1', 'Houston is a city in Texas.')
        def fv(predicate, value):
            return AtomicFact.create(text=f'Houston {predicate} {value}', subject=EntityRef('city', 'Houston'),
                                     predicate=predicate, object_value=FactValue('string', value),
                                     time=FactTime('未注明'),
                                     evidence=(FactEvidence(b1.source.id, 'Houston', 0, 7),))
        memory = MemoryResult({b1.source.id: b1}, (fv('city', 'Houston'), fv('state', 'Texas')))
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        for nid, nd in broken.nodes(data=True):
            if nd.get('etype') == 'City':
                broken.nodes[nid]['state'] = 'California'
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any('state 与事实推导不一致' in e for e in errs))



class ReviewRoundFour(unittest.TestCase):
    """View attribute-set integrity and evidence-grounded source attribution."""

    def seed_two_blocks(self):
        b1 = block('1', '我下周修打印机。')
        b2 = block('2', '别的消息。', speaker='乙', date='2024-05-08')
        return b1, b2

    def test_view_extra_attribute_rejected(self):
        schema = travel_view_schema()
        b1 = block('1', 'Houston x.')
        def fv(predicate, value):
            return AtomicFact.create(text=f'Houston {predicate} {value}', subject=EntityRef('city', 'Houston'),
                                     predicate=predicate, object_value=FactValue('string', value),
                                     time=FactTime('未注明'), evidence=(FactEvidence(b1.source.id, 'Houston', 0, 7),))
        conflicting = (fv('city', 'Houston'), fv('state', 'Texas'), fv('state', 'California'))
        memory = MemoryResult({b1.source.id: b1}, conflicting)
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        for nid, nd in broken.nodes(data=True):
            if nd.get('etype') == 'City':
                broken.nodes[nid]['state'] = 'Texas'  # 补回被冲突规则排除的属性
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any('属性集与事实推导不符' in e and 'state' in e for e in errs))

    def test_view_missing_planned_attribute_rejected(self):
        schema = travel_view_schema()
        b1 = block('1', 'Houston x.')
        def fv(predicate, value):
            return AtomicFact.create(text=f'Houston {predicate} {value}', subject=EntityRef('city', 'Houston'),
                                     predicate=predicate, object_value=FactValue('string', value),
                                     time=FactTime('未注明'), evidence=(FactEvidence(b1.source.id, 'Houston', 0, 7),))
        memory = MemoryResult({b1.source.id: b1}, (fv('city', 'Houston'), fv('state', 'Texas')))
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        for nid, nd in broken.nodes(data=True):
            if nd.get('etype') == 'City':
                del broken.nodes[nid]['state']
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any('缺失' in e and 'state' in e for e in errs))

    def test_fact_sources_swapped_to_other_registered_source_rejected(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1, b2 = self.seed_two_blocks()
        fact = AtomicFact.create(text='甲修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                                 modality='statement', time=FactTime('未注明'),
                                 evidence=(FactEvidence(b1.source.id, '我下周修打印机', 0, 7),))
        memory = MemoryResult({b1.source.id: b1, b2.source.id: b2}, (fact,))
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        broken = nx.MultiDiGraph(graph.graph)
        nid = node_id('AtomicFact', {'id': fact.id})
        broken.nodes[nid]['__sources__'] = [b2.source.id]  # 换成另一个已登记来源
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any('来源追踪与结构不符' in e for e in errs))

    def test_entity_sources_missing_or_extra_rejected(self):
        schema = Schema.from_yaml(SEED.read_text())
        b1, b2 = self.seed_two_blocks()
        fact = AtomicFact.create(text='甲修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                                 modality='statement', time=FactTime('未注明'),
                                 evidence=(FactEvidence(b1.source.id, '我下周修打印机', 0, 7),
                                           FactEvidence(b2.source.id, '别的消息', 0, 4),))
        memory = MemoryResult({b1.source.id: b1, b2.source.id: b2}, (fact,))
        import networkx as nx
        graph = GraphAssembler.build(memory, None, schema)
        ent = node_id('Entity', {'class': 'person', 'name': '甲'})
        broken = nx.MultiDiGraph(graph.graph)
        broken.nodes[ent]['__sources__'] = [b1.source.id]  # 遗漏真实来源 b2
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any('来源追踪与结构不符' in e for e in errs))
        broken.nodes[ent]['__sources__'] = [b1.source.id, b2.source.id, 'unfounded']
        errs = anchoring_invariants(broken, memory.corpus, memory.fingerprint, schema)
        self.assertTrue(any('来源追踪与结构不符' in e for e in errs))

    def test_tool_source_ids_match_fact_evidence_on_good_graph(self):
        from oak.operators.data import DataCapabilities
        schema = Schema.from_yaml(SEED.read_text())
        b1, _ = self.seed_two_blocks()
        fact = AtomicFact.create(text='甲修打印机', subject=EntityRef('person', '甲'), predicate='维修',
                                 modality='statement', time=FactTime('未注明'),
                                 evidence=(FactEvidence(b1.source.id, '我下周修打印机', 0, 7),))
        memory = MemoryResult({b1.source.id: b1}, (fact,))
        graph = GraphAssembler.build(memory, None, schema)
        rows = DataCapabilities(graph).rows
        fact_rows = [r for r in rows.values() if r['entity_type'] == 'AtomicFact']
        self.assertEqual(len(fact_rows), 1)
        self.assertEqual(set(fact_rows[0]['source_ids']), {ev.source_id for ev in fact.evidence})

if __name__ == '__main__':
    unittest.main()
