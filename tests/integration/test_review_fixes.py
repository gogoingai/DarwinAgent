"""Regression coverage for the 2026-10-03 review fixes (P1/P2) and the probe rename scope."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import (AtomicFact, CorpusBlock, EntityRef, EvaluationResult, FactEvidence, FactTime,
                           FactValue, MemoryResult, SourceRef)
from darwinagent.kg.assembler import GraphAssembler, anchoring_invariants
from darwinagent.kg.graph import node_id
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.experiments.spec import precheck_identity
from darwinagent.config import Config as _Cfg, RunConfig as _RC
from darwinagent.kernel.counterexamples import run_probes
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kernel.revision import AssetPatch, AssetRevisionService, parse_training_id, training_id
from darwinagent.operators.sandbox import Interpreter, admit
from darwinagent.runtime.artifacts import digest
from darwinagent.schema.model import Schema

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
            Asset('f_find', 'F', "def run(params):\n rows = nodes('AtomicFact', {'predicate': params.get('predicate','')}, limit=5)\n return [{'node_id': r.get('node_id'), 'text': r.get('text','')} for r in rows]",
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
        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from darwinagent.llm.recorded import RecordedClient
        from darwinagent.contracts import CaseInput, QuestionInput
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
            ev = (training_id('conv-t', 'q1'),)
            with self.assertRaises(ValueError) as caught:
                AssetRevisionService().propose(base, [AssetPatch(dropped, s.fingerprint, '去掉锚定', ev)],
                                               root / 'candidate', list(ev))
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
        (root / 'precheck.json').write_text(json.dumps({'passed': True, 'checks': {},
                                                        'identity': precheck_identity(_Cfg(), _RC(protocol_attempts=1))}))
        controller = RecordedCampaign(root, spec=protocol(rounds=1, cap=1))
        from darwinagent.kernel import TaskSpec
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
        from darwinagent.operators.data import DataCapabilities
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



class GenericFeedbackContract(unittest.TestCase):
    def test_dataset_specific_diagnostics_flow_through(self):
        from darwinagent.experiments.runner import training_feedback
        from darwinagent.contracts import AnswerResult, EvaluationResult, RunResult
        # 旅行式诊断（预算/人数/约束），框架不得丢弃或改读 LoCoMo 字段
        rows = ({'query_id': 'q7', 'budget_exceeded': True, 'people': 3,
                 'constraint': 'no flight', 'plan_issues': ['超预算']},)
        ev = (SourceRef('message_text', 'c', '7'),)
        class C:  id='c'
        result = RunResult('c', 'id', 'v', (AnswerResult('q7', 'answered', 'ok', evidence=ev),), 5)
        baseline = EvaluationResult({'feasible': 0, 'budget_ok': 1}, 1, 1, 0, 0, rows)
        feedback = training_feedback([C()], [result], [('c', rows)], baseline)
        self.assertEqual(feedback['diagnostics'][0]['case_id'], 'c')
        self.assertEqual(feedback['diagnostics'][0]['diagnostic']['constraint'], 'no flight')
        self.assertEqual(feedback['diagnostic_rows_total'], 1)
        self.assertIn('feasible', feedback['scores']['metrics'])

    def test_passed_rows_skipped_and_budget_capped(self):
        from darwinagent.experiments.runner import training_feedback, FEEDBACK_BUDGET_CHARS
        from darwinagent.contracts import AnswerResult, EvaluationResult, RunResult
        rows = tuple({'i': i, 'passed': True, 'payload': 'x'} for i in range(3)) + \
               ({'i': 9, 'payload': 'y' * 100},)
        big = {'i': 10, 'payload': 'z' * (FEEDBACK_BUDGET_CHARS + 10)}
        rows = rows + (big,)
        class C:  id='c'
        result = RunResult('c', 'id', 'v', (), 0)
        baseline = EvaluationResult({'m': 0}, 0, 0, 0, 0, rows)
        feedback = training_feedback([C()], [result], [('c', rows)], baseline)
        # 新契约（评审#2）：未识别结构的超长行压缩入载（_row_truncated 前缀），总预算仍受控
        self.assertEqual([r['diagnostic']['i'] for r in feedback['diagnostics'] if 'i' in r['diagnostic']], [9])
        self.assertTrue(any('_row_truncated' in r['diagnostic'] for r in feedback['diagnostics']))
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        # 新契约：计数只含失败行（3 条 passed 不计），9 号与压缩后的超长行共 2 条
        self.assertEqual(feedback['diagnostic_rows_total'], 2)



class ReviewRoundSix(unittest.TestCase):
    """P1 resume keeps adopted rounds; P2 one budget for the whole payload; P3 composite
    question identity through admission; P4 per-case stage aggregation."""

    def test_resume_with_stop_restores_adopted_round_and_pointer(self):
        import asyncio
        from tests.integration.test_experiment import RecordedExperiment
        import contextlib, io
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        runner = RecordedExperiment(root)
        from darwinagent.kernel import TaskSpec
        task = TaskSpec.load(Path(__file__).resolve().parents[2] / 'tasks/device_maintenance/task.yaml')
        with contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.run(runner.run(runner.case.id, task, rounds=1))
        self.assertEqual([d['accepted'] for d in first['rounds']], [True])
        adopted_v = first['adopted_version']
        (root / 'STOP').write_text('operator stop\n')
        with contextlib.redirect_stdout(io.StringIO()):
            again = asyncio.run(runner.run(runner.case.id, task, rounds=1, resume=True,
                                           stop_file=root / 'STOP'))
        # 恢复先还原完整决策史：R1 仍在、指针仍指 R1、无新提案
        self.assertEqual([d['accepted'] for d in again['rounds']], [True])
        self.assertEqual(again['adopted_version'], adopted_v)
        self.assertTrue(again['stopped_by_operator'])
        pointer = json.loads((root / 'published' / 'current.json').read_text())
        self.assertEqual(pointer['version'], adopted_v)
        self.assertFalse((root / 'R2').exists())

    def test_feedback_budget_covers_entire_payload(self):
        from darwinagent.experiments.runner import training_feedback, FEEDBACK_BUDGET_CHARS
        from darwinagent.contracts import AnswerResult, EvaluationResult, RunResult, SourceRef
        class C:  id='c'
        ev = (SourceRef('t', 'c', '1'),)
        huge_rows = tuple({'i': i, 'payload': 'x' * 2000} for i in range(40))
        failures_mass = [AnswerResult(f'q{i}', 'execution_error', '', error='E' * 5000)
                         for i in range(20)]
        result = RunResult('c', 'id', 'v', tuple(failures_mass), 0)
        baseline = EvaluationResult({'m': 0}, 20, 0, 20, 0, huge_rows)
        feedback = training_feedback([C()], [result], [('c', huge_rows)], baseline)
        self.assertNotIn('diagnostics', feedback['scores'])
        # 上限以完整序列化载荷为准（含骨架/字段名/统计），不得放宽
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        self.assertTrue(feedback['generation_failures_truncated'])
        self.assertEqual(feedback['generation_failures_total'], 20)

    def test_composite_question_identity_in_admission(self):
        from darwinagent.kernel.revision import AssetPatch, AssetRevisionService
        from tests.fixtures import spec
        from dataclasses import replace
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name); s = spec(root / 'assets')
        a = s.bundle.get('answer_prompt')
        composite = [training_id('case-a', 'q1'), training_id('case-b', 'q1')]
        svc = AssetRevisionService()
        # 不可解析（裸 id / 无长度前缀）与可解析但不存在的依据都被拒绝
        for bad in (['q1'], ['case-a::q1'], [training_id('case-a', 'qX')]):
            with self.assertRaises(ValueError):
                svc.propose(s.bundle, [AssetPatch(replace(a, content='Be precise.'), a.fingerprint,
                                                  'r', tuple(bad))], root / 'cand', composite)
        good = svc.propose(s.bundle, [AssetPatch(replace(a, content='Be precise.'), a.fingerprint,
                                                 'r', (training_id('case-b', 'q1'),))], root / 'cand2', composite)
        self.assertNotEqual(good.version, s.bundle.version)

    def test_stage_statuses_per_case_layout(self):
        from tests.integration.test_campaign import RecordedCampaign, protocol
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        v1 = root / 'validation' / 'v1'
        for case, status in (('val-case', 'complete'), ('val-case-2', 'failed')):
            d = v1 / case; d.mkdir(parents=True)
            (d / 'stage.json').write_text(json.dumps({'status': status}))
        statuses = controller._stage_statuses()
        self.assertEqual(statuses['validation/v1/val-case']['status'], 'complete')
        unhealthy = {k: v for k, v in statuses.items() if v['status'] != 'complete'}
        self.assertEqual(list(unhealthy), ['validation/v1/val-case-2'])
        self.assertEqual(unhealthy['validation/v1/val-case-2']['version'], 'v1')
        self.assertEqual(unhealthy['validation/v1/val-case-2']['case'], 'val-case-2')

class StageTaggedEvaluator:
    """Every stage's evaluation carries exactly one diagnostic row tagged with its stage."""

    def __init__(self, stage):
        stage = Path(stage)
        name = stage.parent.name
        self.stage = name if name == 'B0' or name.startswith('R') else stage.parent.parent.name

    async def evaluate(self, result, asked=None):
        assert all(a.status == 'answered' for a in result.answers)
        diag = ({'question_id': result.answers[0].question_id, 'stage_tag': self.stage},)
        return EvaluationResult({'precise': 0 if self.stage == 'B0' else 1}, 1, 1, 0, 0, diag)


class ReviewRoundSeven(unittest.TestCase):
    """P1 proposal diagnostics follow the last ADOPTED stage; P2 the complete serialized
    payload respects the budget; P2 the training identity encoding is collision-free."""

    @staticmethod
    def feedback_for(root, stage):
        return json.loads((root / stage / 'proposal-call.json').read_text())['input']['task_training_feedback']

    def run_rounds(self, root, rounds, resume=False, runner_cls=None):
        from tests.integration.test_experiment import RecordedExperiment
        from tests.fixtures import TASK
        import contextlib, io
        from darwinagent.kernel import TaskSpec
        cls = runner_cls or RecordedExperiment
        runner = cls(root, evaluator=lambda transport, path: StageTaggedEvaluator(path))
        spec = TaskSpec.load(TASK / 'task.yaml')
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(runner.run(runner.case.id, spec, rounds=rounds, resume=resume)), runner

    def test_proposal_diagnostics_follow_last_adopted(self):
        # 完整提案流程（非单测 training_feedback）：R1 的诊断来自 B0；R1 采纳后 R2 来自 R1；
        # R2 被拒后 R3 仍来自 R1。提案输入的题目身份可解码回原题。
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        summary, runner = self.run_rounds(root, 3)
        self.assertEqual([d['accepted'] for d in summary['rounds']], [True, False, False])
        r1 = self.feedback_for(root, 'R1')
        self.assertEqual([r['diagnostic']['stage_tag'] for r in r1['diagnostics']], ['B0'])
        self.assertEqual(r1['diagnostic_rows_total'], 1)
        r2 = self.feedback_for(root, 'R2')
        self.assertEqual([r['diagnostic']['stage_tag'] for r in r2['diagnostics']], ['R1'])
        r3 = self.feedback_for(root, 'R3')
        self.assertEqual([r['diagnostic']['stage_tag'] for r in r3['diagnostics']], ['R1'])
        questions = json.loads((root / 'R1' / 'proposal-call.json').read_text())['input']['questions']
        self.assertEqual(parse_training_id(questions[0]['training_id']),
                         (runner.case.id, runner.case.questions[0].id))

    def test_resume_keeps_diagnostic_source(self):
        from tests.integration.test_experiment import RecordedExperiment

        class CrashedBeforeR3(RecordedExperiment):
            """First process dies right before the R3 proposal; resume must replay history."""
            armed = True

            def _client(self, stage):
                if self.armed and stage == 'R3':
                    raise RuntimeError('中断：R3 提案前')
                return super()._client(stage)

        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        with self.assertRaises(RuntimeError):
            self.run_rounds(root, 3, runner_cls=CrashedBeforeR3)
        again, _ = self.run_rounds(root, 3, resume=True)
        self.assertEqual([d['accepted'] for d in again['rounds']], [True, False, False])
        # 恢复后新提案（R3）的诊断仍来自最后采纳版本 R1，而非尚未评测的 R3
        r3 = self.feedback_for(root, 'R3')
        self.assertEqual([r['diagnostic']['stage_tag'] for r in r3['diagnostics']], ['R1'])

    def test_many_short_records_bounded_by_complete_payload(self):
        from darwinagent.experiments.runner import training_feedback, FEEDBACK_BUDGET_CHARS
        from darwinagent.contracts import EvaluationResult, RunResult
        class C:  id = 'c'
        rows = tuple({'i': i, 'payload': 'x' * 60} for i in range(3000))
        baseline = EvaluationResult({'m': 0}, 0, 0, 0, 0, rows)
        feedback = training_feedback([C()], [RunResult('c', 'id', 'v', (), 0)], [('c', rows)], baseline)
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        self.assertGreater(feedback['diagnostic_rows_in_proposal'], 0)
        self.assertEqual(feedback['diagnostic_rows_total'], 3000)

    def test_few_long_records_bounded_by_complete_payload(self):
        from darwinagent.experiments.runner import training_feedback, FEEDBACK_BUDGET_CHARS
        from darwinagent.contracts import EvaluationResult, RunResult
        class C:  id = 'c'
        rows = tuple({'i': i, 'payload': 'y' * 20000} for i in range(8))
        baseline = EvaluationResult({'m': 0}, 0, 0, 0, 0, rows)
        feedback = training_feedback([C()], [RunResult('c', 'id', 'v', (), 0)], [('c', rows)], baseline)
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        # 新契约（评审#2）：超长未识别结构行压缩入载，8 条全部可进且总预算受控
        self.assertEqual(feedback['diagnostic_rows_in_proposal'], 8)
        self.assertEqual(feedback['diagnostic_rows_total'], 8)
        self.assertTrue(all('_row_truncated' in r['diagnostic'] for r in feedback['diagnostics']))

    def test_mixed_sections_bounded_by_complete_payload(self):
        from darwinagent.experiments.runner import training_feedback, FEEDBACK_BUDGET_CHARS
        from darwinagent.contracts import AnswerResult, EvaluationResult, RunResult
        class C:  id = 'c'
        rows = tuple({'i': i, 'payload': 'd' * 300} for i in range(200))
        failures = tuple(AnswerResult(f'q{i}', 'execution_error', '', error='E' * 400) for i in range(30))
        graph_rows = tuple({'g': i, 'detail': 'G' * 200} for i in range(50))
        result = RunResult('c', 'id', 'v', failures, 0, graph_diagnostics=graph_rows)
        baseline = EvaluationResult({'m': 0}, 30, 0, 30, 0, rows)
        feedback = training_feedback([C()], [result], [('c', rows)], baseline)
        self.assertLessEqual(len(json.dumps(feedback, ensure_ascii=False)), FEEDBACK_BUDGET_CHARS)
        # 优先级保持：诊断先填满，之后才轮到故障与图诊断
        self.assertGreater(feedback['diagnostic_rows_in_proposal'], 0)
        self.assertLess(feedback['diagnostic_rows_in_proposal'], 200)
        self.assertTrue(feedback['generation_failures_truncated'])
        self.assertEqual(feedback['generation_failures_total'], 30)

    def test_training_id_is_collision_free_and_parseable(self):
        # 评审给出的两个合法输入：旧的 'a::b' 拼接会碰撞，长度前缀编码必须区分并精确还原
        a = training_id('case-a::part', 'q1')
        b = training_id('case-a', 'part::q1')
        self.assertNotEqual(a, b)
        self.assertEqual(parse_training_id(a), ('case-a::part', 'q1'))
        self.assertEqual(parse_training_id(b), ('case-a', 'part::q1'))
        for bad in ('q1', 'case-a::q1', '6:case-a::', 'x:case-a::q1', '99:case-a::q1', ''):
            with self.assertRaises(ValueError):
                parse_training_id(bad)

    def test_question_identity_uses_the_encoder(self):
        from darwinagent.experiments.runner import question_identity
        from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
        b = CorpusBlock(SourceRef('message_text', 'c', '1'), '文本。')
        case = CaseInput('case-a', (b,), (QuestionInput('q::1', '问题'), QuestionInput('q1', '问题2')))
        ids = question_identity(case)
        self.assertEqual([parse_training_id(t) for t in ids],
                         [('case-a', 'q::1'), ('case-a', 'q1')])


class FaultyStageEvaluator(StageTaggedEvaluator):
    """R1's evaluation cannot finish scoring: incomplete answers plus an evaluation fault."""

    async def evaluate(self, result, asked=None):
        if self.stage == 'R1':
            return EvaluationResult({'precise': 0}, 1, 0, 1, 1)
        return await super().evaluate(result)


class ReviewRoundEight(unittest.TestCase):
    """P1 train-stage faults reach the totals; P2 the report persists before sealing and
    rebuilds from artifacts; P2 an oversized scores skeleton refuses the proposal; P2 the
    revision protocol matches the bundle's real graph mode."""

    def test_train_stage_faults_surface_in_run_status(self):
        from tests.integration.test_experiment import RecordedExperiment
        from tests.fixtures import TASK
        import contextlib, io
        from darwinagent.kernel import TaskSpec
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        runner = RecordedExperiment(root, evaluator=lambda transport, path: FaultyStageEvaluator(path))
        with contextlib.redirect_stdout(io.StringIO()):
            summary = asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / 'task.yaml'), rounds=2))
        # R1 因故障无法完成评分：候选被拒是正常结果，但故障必须进入总状态
        self.assertEqual(summary['unhealthy_stages']['R1']['evaluation_faults'], 1)
        self.assertEqual(summary['unhealthy_stages']['R1']['completed'], 0)
        self.assertEqual(summary['status'], 'failed')

    def test_train_stages_surface_in_campaign_statuses(self):
        from tests.integration.test_campaign import RecordedCampaign, protocol
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        train = root / 'train'
        (train / 'B0').mkdir(parents=True)
        (train / 'B0' / 'stage.json').write_text(json.dumps(
            {'status': 'complete', 'cases': ['train-case'], 'asset_version': 'b0',
             'scores': {'completed': 1, 'total': 1, 'generation_faults': 0, 'evaluation_faults': 0}}))
        (train / 'R1').mkdir(parents=True)
        (train / 'R1' / 'stage.json').write_text(json.dumps(
            {'status': 'failed', 'cases': ['train-case'], 'asset_version': 'r1',
             'scores': {'completed': 0, 'total': 1, 'generation_faults': 1, 'evaluation_faults': 1}}))
        statuses = controller._stage_statuses()
        self.assertEqual(statuses['train/B0']['status'], 'complete')
        unhealthy = {k: v for k, v in statuses.items() if v['status'] != 'complete'}
        self.assertIn('train/R1', unhealthy)
        self.assertEqual(unhealthy['train/R1']['case'], 'train-case')

    def test_report_persisted_before_seal_and_rebuilds_from_artifacts(self):
        from tests.integration.test_campaign import RecordedCampaign, protocol
        from tests.fixtures import TASK
        from darwinagent.kernel import TaskSpec
        from unittest import mock
        import contextlib, io
        import darwinagent.experiments.campaign as camp
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        task = TaskSpec.load(TASK / 'task.yaml')
        (root / 'precheck.json').write_text(json.dumps(
            {'passed': True, 'checks': {}, 'identity': precheck_identity(_Cfg(), _RC(protocol_attempts=1))}))
        real_atomic = camp.atomic_json

        def flaky(path, data):
            if Path(path).name == 'campaign-summary.json':
                raise OSError('disk full')
            return real_atomic(path, data)

        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(camp, 'atomic_json', side_effect=flaky):
                with self.assertRaises(OSError):
                    asyncio.run(controller.run(task))
        # 报告未落盘时绝不封存：phase 停在 test，可恢复
        self.assertEqual(json.loads((root / 'campaign.json').read_text())['phase'], 'test')
        self.assertFalse((root / 'campaign-summary.json').exists())
        controller2 = RecordedCampaign(root, spec=protocol(rounds=1))
        with contextlib.redirect_stdout(io.StringIO()):
            summary2 = asyncio.run(controller2.run(task, resume=True))
        self.assertEqual(json.loads((root / 'campaign.json').read_text())['phase'], 'done')
        # 已封存但报告丢失：只从产物重建报告，不重新执行（台账不变、内容一致）
        (root / 'campaign-summary.json').unlink()
        ledger_before = (root / 'question_runs.jsonl').read_text()
        controller3 = RecordedCampaign(root, spec=protocol(rounds=1))
        with contextlib.redirect_stdout(io.StringIO()):
            summary3 = asyncio.run(controller3.run(task, resume=True))
        self.assertEqual(summary3, summary2)
        self.assertEqual((root / 'question_runs.jsonl').read_text(), ledger_before)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(controller3.run(task, resume=True))
        self.assertIn('sealed', str(caught.exception))

    def test_oversize_scores_skeleton_refuses_feedback(self):
        from darwinagent.experiments.runner import training_feedback
        from darwinagent.contracts import EvaluationResult, RunResult
        class C:  id = 'c'
        huge = EvaluationResult({f'm{i}': 0 for i in range(4000)}, 0, 0, 0, 0, ())
        with self.assertRaises(ValueError) as caught:
            training_feedback([C()], [RunResult('c', 'id', 'v', (), 0)], [('c', ())], huge)
        self.assertIn('预算', str(caught.exception))

    def anchored_bundle(self, root):
        return AnchoredPipelineChecksTaskC().bundle(root / 'anchored')

    def test_revision_protocol_matches_bundle_mode(self):
        from darwinagent.experiments.bootstrap import revision_protocol
        from tests.fixtures import spec
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        legacy = revision_protocol(spec(root / 'assets').bundle)
        self.assertNotIn('fact-anchored', legacy)
        self.assertNotIn('AtomicFact', legacy)
        self.assertIn('"patches"', legacy)
        self.assertNotIn('{"assets"', legacy)
        self.assertIn('S may be revised', legacy)
        anchored = revision_protocol(self.anchored_bundle(root))
        self.assertIn('fact-anchored', anchored)
        self.assertIn('EXTEND', anchored)
        self.assertIn('"patches"', anchored)
        self.assertNotIn('{"assets"', anchored)
        self.assertNotIn('do not generate, extend or patch it', anchored)  # 矛盾指令已消除

    def test_bootstrap_protocols_carry_single_output_format(self):
        from darwinagent.experiments.bootstrap import ASSET_PROTOCOL, LEGACY_ASSET_PROTOCOL
        for proto in (ASSET_PROTOCOL, LEGACY_ASSET_PROTOCOL):
            self.assertIn('shaped {"assets":[...]}', proto)
            self.assertNotIn('patches', proto)

    def test_proposal_call_records_mode_consistent_protocol(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        self.run_rounds_proxy(root)
        recorded = json.loads((root / 'R1' / 'proposal-call.json').read_text())
        self.assertIn('protocol', recorded)
        # device 任务（legacy 图模式）的修订协议不得出现原子图指令
        self.assertNotIn('AtomicFact', recorded['protocol'])
        self.assertNotIn('fact-anchored', recorded['protocol'])

    def run_rounds_proxy(self, root):
        from tests.integration.test_experiment import RecordedExperiment
        from tests.fixtures import TASK
        import contextlib, io
        from darwinagent.kernel import TaskSpec
        runner = RecordedExperiment(root)
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / 'task.yaml'), rounds=1))


if __name__ == '__main__':
    unittest.main()
