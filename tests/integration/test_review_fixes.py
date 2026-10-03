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


if __name__ == '__main__':
    unittest.main()
