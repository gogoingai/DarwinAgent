"""评审修复回归（2026-10-04）：分批重试统计、检索工具底线、提案错误回灌、外测聚合。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oak.contracts import AnswerResult, EvaluationResult, RunResult, SourceRef
from oak.kernel.assets import Asset
from oak.kernel.validation import capability_floor_errors, capability_names
from oak.llm.recorded import RecordedClient
from oak.runtime.artifacts import digest


class BatchedFaultRetryTests(unittest.TestCase):
    def run_retry(self, script, faulted):
        from oak.experiments.runner import batched_fault_retry
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        answers_dir = Path(td.name)
        for a in faulted:
            (answers_dir / f'{digest(a.question_id)}.json').write_text('{}')
        calls = []
        class FakePipeline:
            async def run(self, case, spec, config):
                calls.append(1)
                return script.pop(0)
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        result, still = asyncio.run(batched_fault_retry(
            FakePipeline(), 'c', None, None, answers_dir, faulted,
            sleep=fake_sleep, batch_size=25, lead_s=0.0, gap_s=0.0))
        return result, still, calls, sleeps, answers_dir

    def test_all_recovered_final_set_recomputed(self):
        # 评审#4 离线复现场景：30 题故障，第一批重跑后其余 5 题仍带旧故障检查点，
        # 全部重跑后恢复。最终统计必须出自最后一份答案集（旧实现取并集误报 5 题）。
        faulted = [AnswerResult(f'q{i}', 'execution_error', '', error='x') for i in range(30)]
        ev=(SourceRef('message_text','c','1'),)
        healthy = [AnswerResult(f'q{i}', 'answered', 'ok', evidence=ev) for i in range(30)]
        rerun1 = RunResult('c', 'id', 'v', tuple(healthy[:25] + faulted[25:]), 0)
        rerun2 = RunResult('c', 'id', 'v', tuple(healthy), 0)
        result, still, calls, sleeps, answers_dir = self.run_retry([rerun1, rerun2], faulted)
        self.assertEqual(calls, [1, 1])                 # 两批各重跑一次
        self.assertEqual(still, [])                     # 并集实现会留下 q25..q29
        self.assertEqual(result, rerun2)                # 返回最后一份完整答案集
        self.assertEqual(sleeps, [0.0, 0.0])           # lead+gap 各一次（均为 0 秒）
        self.assertFalse(any(answers_dir.glob('*.json')))  # 30 个故障检查点全部删除

    def test_persistent_faults_reported_from_final_set(self):
        faulted = [AnswerResult('q1', 'execution_error', '', error='x'),
                   AnswerResult('q2', 'execution_error', '', error='y')]
        final = RunResult('c', 'id', 'v', (AnswerResult('q1', 'answered', 'ok', evidence=(SourceRef('m','c','1'),)),
                                           AnswerResult('q2', 'execution_error', '', error='y')), 0)
        result, still, *_ = self.run_retry([final], faulted)
        self.assertEqual(still, ['q2'])
        self.assertEqual(result, final)


class RetrievalFloorTests(unittest.TestCase):
    def F(self, fid, src):
        return Asset(fid, 'F', src, {'type': 'any'}, {'type': 'any'}, trial_inputs=({'q': 'x'},))

    def test_capability_names_mapping(self):
        self.assertEqual(capability_names({'semantic_search': True, 'traversal': True}),
                         ('semantic_search', 'traverse'))
        self.assertEqual(capability_names({'semantic_search': False}), ())
        self.assertEqual(capability_names({}), ())

    class _Assets:  # 能力底线只看 F 集，不需要完整 bundle
        def __init__(self, *fs): self.assets = fs

    def test_floor_requires_both_classes(self):
        keyword_only = self._Assets(self.F('search', 'def run(p):\n return search(p["q"])'))
        missing = capability_floor_errors(keyword_only, ('semantic_search', 'traverse'))
        self.assertEqual(len(missing), 2)               # 关键词 search 不满足任何一类
        with_vec = self._Assets(self.F('v', 'def run(p):\n return semantic_search(p["q"])'),
                                self.F('t', 'def run(p):\n return traverse(p["id"])'))
        self.assertEqual(capability_floor_errors(with_vec, ('semantic_search', 'traverse')), [])


class ProposalFeedbackTests(unittest.TestCase):
    def locomo_row(self, qid, precise):
        return {'question_id': qid, 'question': f'Q{qid}', 'status': 'answered',
                'answer': 'a' * 300, 'error': None,
                'original': {'idx': 0, 'key': 'k', 'status': 'ok', 'id': 'c0',
                             'lenient': precise, 'precise': precise,
                             'missing_elements': [], 'wrong_elements': ['缺' * 80],
                             'precision_issues': [], 'reference_items': ['GOLD-SECRET']}}

    def test_failure_first_compressed_with_trace(self):
        from oak.experiments import runner as R
        rows = (self.locomo_row('0', True), self.locomo_row('1', False),
                self.locomo_row('2', False))
        ev = (SourceRef('message_text', 'c', '1'),)
        answers = (AnswerResult('0', 'answered', 'ok', evidence=ev),
                   AnswerResult('1', 'answered', 'wrong', evidence=ev,
                                raw_outputs=('{"action":"call","asset_id":"search_facts","parameters":{"terms":["x"]}}',)),
                   AnswerResult('2', 'execution_error', '', error='ProtocolError: boom'))
        result = RunResult('c', 'id', 'v', answers, 5)
        class C: id = 'c'
        baseline = EvaluationResult({'original_precise': 1}, 2, 3, 1, 0, rows)
        feedback = R.training_feedback([C()], [result], [('c', rows)], baseline)
        ids = [r['diagnostic']['question_id'] for r in feedback['diagnostics']]
        self.assertEqual(ids, ['1', '2'])               # 通过题不进反馈（失败优先）
        blob = json.dumps(feedback, ensure_ascii=False)
        self.assertNotIn('GOLD-SECRET', blob)           # 金标不进提案载荷
        self.assertNotIn('reference_items', blob)
        traces = {r['diagnostic']['question_id']: r.get('trace') for r in feedback['diagnostics']}
        self.assertEqual(traces['1']['tool_calls'], 1)
        self.assertEqual(traces['1']['actions'][0]['tool'], 'search_facts')
        self.assertTrue(traces['1']['actions'][0]['params'].startswith('{"terms"'))
        self.assertEqual(traces['2']['status'], 'execution_error')
        self.assertEqual(feedback['diagnostic_rows_total'], 2)

    def test_budget_rotates_across_cases(self):
        from oak.experiments import runner as R
        rows_a = tuple(self.locomo_row(str(i), False) for i in range(6))
        rows_b = tuple(self.locomo_row(str(i), False) for i in range(6))
        answers = tuple(AnswerResult(str(i), 'answered', 'x', evidence=(SourceRef('m', 'c', '1'),)) for i in range(6))
        class C1: id = 'a'
        class C2: id = 'b'
        results = [RunResult('a', 'i', 'v', answers, 1), RunResult('b', 'i2', 'v', answers, 1)]
        baseline = EvaluationResult({'original_precise': 0}, 0, 12, 0, 0, rows_a + rows_b)
        with mock.patch.object(R, 'FEEDBACK_BUDGET_CHARS', 2600):
            feedback = R.training_feedback([C1(), C2()], results, [('a', rows_a), ('b', rows_b)], baseline)
        per_case = {}
        for r in feedback['diagnostics']:
            per_case.setdefault(r['case_id'], 0)
            per_case[r['case_id']] += 1
        self.assertEqual(sorted(per_case), ['a', 'b'])   # 预算紧张时轮转：两个对话都有行
        self.assertLess(sum(per_case.values()), 12)


class ProposalPayloadContract(unittest.TestCase):
    def test_scope_and_admission_error_reach_the_model(self):
        from oak.experiments.proposal import ProposalGenerator
        from tests.fixtures import case, spec
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        s = spec(root / 'assets')
        client = RecordedClient({'proposal': ['not-json'] * 6})
        config = __import__('oak.config', fromlist=['RunConfig']).RunConfig()
        async def run():
            return await ProposalGenerator().propose(
                s.bundle if hasattr(s, 'bundle') else None, [case()], {'x': 1}, client, config,
                root / 'call.json', [{'training_id': '2::c::q1', 'text': 't'}],
                allowed_kinds=('P',), admission_error='ValueError: Stale baseline or asset type change')
        with self.assertRaises(Exception):
            asyncio.run(run())
        recorded = json.loads((root / 'call.json').read_text())
        self.assertEqual(recorded['input']['allowed_asset_kinds'], ['P'])
        self.assertIn('Stale baseline', recorded['input']['previous_admission_error'])
        self.assertIn('may only patch', recorded['protocol'])


class ExternalReportTests(unittest.TestCase):
    def test_macro_micro_and_delta(self):
        from datasets.locomo.scripts.external_test import aggregate_report
        g1 = [{'case_id': 'conv-30', 'original_precise': 100, 'original_lenient': 110,
               'repaired_precise': 101, 'repaired_lenient': 111, 'completed': 195, 'total': 200,
               'generation_faults': 5, 'evaluation_faults': 0},
              {'case_id': 'conv-41', 'original_precise': 80, 'original_lenient': 90,
               'repaired_precise': 81, 'repaired_lenient': 91, 'completed': 150, 'total': 150,
               'generation_faults': 0, 'evaluation_faults': 0}]
        v0 = [{'case_id': 'conv-30', 'original_precise': 90, 'original_lenient': 100,
               'repaired_precise': 90, 'repaired_lenient': 100, 'completed': 200, 'total': 200,
               'generation_faults': 0, 'evaluation_faults': 0},
              {'case_id': 'conv-41', 'original_precise': 90, 'original_lenient': 95,
               'repaired_precise': 90, 'repaired_lenient': 95, 'completed': 150, 'total': 150,
               'generation_faults': 0, 'evaluation_faults': 0}]
        report = aggregate_report(g1, v0)
        self.assertEqual(report['macro']['original_precise'], 90.0)      # (100+80)/2 等权
        self.assertEqual(report['micro']['original_precise_sum'], 180)
        deltas = {d['case_id']: d for d in report['delta_vs_baseline']}
        self.assertEqual(deltas['conv-30']['original_precise_delta'], 10)
        self.assertEqual(deltas['conv-41']['original_precise_delta'], -10)
        self.assertEqual(report['macro']['delta_vs_baseline']['original_precise'], 0.0)
        self.assertEqual(report['micro']['generation_faults'], 5)


if __name__ == '__main__':
    unittest.main()
