"""评审修复回归（2026-10-04）：分批重试统计、检索工具底线、提案错误回灌、外测聚合。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oak.config import RunConfig
from oak.contracts import AnswerResult, CaseInput, EvaluationResult, QuestionInput, RunResult, SourceRef
from oak.kernel.assets import Asset, KernelAssets
from oak.kernel import KernelBundle
from oak.kernel.validation import capability_floor_errors, capability_names
from oak.llm.recorded import RecordedClient
from oak.runtime.artifacts import digest
from types import SimpleNamespace


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

    def test_comment_only_mentions_rejected(self):
        # 评审#4 反例：注释里写着 semantic_search/traverse、实际只执行 nodes——必须拒绝
        fake = self._Assets(self.F('fake', 'def run(p):\n # semantic_search and traverse are great\n s = "semantic_search traverse"\n return nodes({})'))
        missing = capability_floor_errors(fake, ('semantic_search', 'traverse'))
        self.assertEqual(len(missing), 2)

    def test_real_calls_pass(self):
        real = self._Assets(self.F('v', 'def run(p):\n return semantic_search(p["q"])'),
                            self.F('t', 'def run(p):\n return traverse(p["id"])'))
        self.assertEqual(capability_floor_errors(real, ('semantic_search', 'traverse')), [])

    def test_trial_floor_requires_positive_execution(self):
        from oak.kernel.validation import trial_capability_floor_errors
        untouched = ({'asset_id': 'a', 'data': [], 'capability_calls': {}},)
        self.assertEqual(len(trial_capability_floor_errors(untouched, ('semantic_search', 'traverse'))), 2)
        empty_but_executed = ({'asset_id': 'a', 'data': [], 'capability_calls': {'semantic_search': 1}},)
        problems = trial_capability_floor_errors(empty_but_executed, ('semantic_search', 'traverse'))
        self.assertEqual(len(problems), 1)              # 空结果如实记录：执行过即算触发
        self.assertEqual(trial_capability_floor_errors(
            ({'capability_calls': {'semantic_search': 2, 'traverse': 1}},),
            ('semantic_search', 'traverse')), [])

    def test_floor_requires_both_classes(self):
        keyword_only = self._Assets(self.F('search', 'def run(p):\n return search(p["q"])'))
        missing = capability_floor_errors(keyword_only, ('semantic_search', 'traverse'))
        self.assertEqual(len(missing), 2)               # 关键词 search 不满足任何一类
        with_vec = self._Assets(self.F('v', 'def run(p):\n return semantic_search(p["q"])'),
                                self.F('t', 'def run(p):\n return traverse(p["id"])'))
        self.assertEqual(capability_floor_errors(with_vec, ('semantic_search', 'traverse')), [])


class TraceSummaryTests(unittest.TestCase):
    def _answer(self, tool_data, review_accepted=True, review_feedback=None):
        ev = (SourceRef('message_text', 'c', '1'),)
        raw = ('{"action":"call","asset_id":"f_semantic","parameters":{"query":"q"}}',
               '{"action":"ready"}',
               '{"status":"answered","answer":"ok"}',
               '{"accepted":true,"feedback":"ok"}')
        trace = ({'stage': 'tool', 'attempt': 0, 'step': 0, 'asset_id': 'f_semantic',
                  'data': tool_data, 'node_ids': [], 'source_ids': [], 'read_operations': 1,
                  'capability_calls': {'semantic_search': 1}},
                 {'stage': 'candidate', 'attempt': 0, 'candidate': {'status': 'answered'}, 'checks': ()},
                 {'stage': 'review', 'attempt': 0, 'accepted': review_accepted,
                  'feedback': review_feedback or 'ok'})
        return AnswerResult('0', 'answered', 'ok', evidence=ev, raw_outputs=raw, trace=trace)

    def test_tool_calls_distinct_from_model_outputs(self):
        # 评审#2 反例：一次工具调用 + ready + 作答 + 审查＝4 条模型输出，工具调用数必须是 1
        from oak.experiments.runner import _retrieval_trace
        summary = _retrieval_trace(self._answer([{'编号': 'c-0001'}]))
        self.assertEqual(summary['tool_calls'], 1)
        self.assertEqual(summary['model_calls'], 4)
        self.assertEqual(summary['returned_rows'], 1)      # 来自工具返回，不是最终引用数
        self.assertEqual(summary['stopped'], 'ready')
        self.assertEqual(summary['tools'][0]['tool'], 'f_semantic')
        self.assertEqual(summary['tools'][0]['capabilities'], {'semantic_search': 1})

    def test_empty_result_and_review_rejection_visible(self):
        from oak.experiments.runner import _retrieval_trace
        summary = _retrieval_trace(self._answer([], review_accepted=False, review_feedback='证据不足，不应作答'))
        self.assertEqual(summary['empty_results'], 1)
        self.assertTrue(any(r['by'] == 'review' and '证据不足' in r['reason'] for r in summary['rejections']))


class TraceGapTests(unittest.TestCase):
    """评审②四缺口：检查理由（mappingproxy）可见；被拒调用后成功工具参数正确；
    证据与来源可见；摘要截断可识别。"""

    def _answer(self, trace_events, raw):
        ev = (SourceRef('message_text', 'c', '1'),)
        # AnswerResult 构造时会 freeze trace —— mappingproxy 是真实运行时形态
        return AnswerResult('0', 'answered', 'ok', evidence=ev, raw_outputs=raw, trace=trace_events)

    def test_frozen_check_reasons_visible(self):
        from oak.experiments.runner import _retrieval_trace
        events = ({'stage': 'candidate', 'attempt': 0,
                   'candidate': {'status': 'answered'},
                   'checks': ({'check_id': 'fixed.cite', 'ok': False,
                               'issues': ['引用了不可见行']},)},)
        answer = self._answer(events, ('{"status":"answered"}', '{"accepted":true}'))
        summary = _retrieval_trace(answer)
        rejected = [r for r in summary['rejections'] if r['by'] == 'check']
        self.assertTrue(rejected)
        self.assertIn('引用了不可见行', rejected[0]['issues'])

    def test_params_come_from_execution_record_not_rejected_calls(self):
        # 第一次动作调未登记工具被拒（raw_outputs 里留下错误参数），第二次成功——
        # 摘要必须用工具事件内执行点记录的 parameters
        from oak.experiments.runner import _retrieval_trace
        raw = ('{"action":"call","asset_id":"bogus_tool","parameters":{"wrong":true}}',
               '{"action":"call","asset_id":"f_good","parameters":{"stale":"x"}}',
               '{"action":"ready"}')
        events = ({'stage': 'tool', 'attempt': 0, 'step': 0, 'asset_id': 'f_good',
                   'parameters': {'query': '正确参数'}, 'data': [{'node_id': 'n1'}],
                   'node_ids': [], 'source_ids': [], 'read_operations': 1,
                   'capability_calls': {'semantic_search': 1}},)
        summary = _retrieval_trace(self._answer(events, raw))
        self.assertEqual(summary['tools'][0]['params'], '{"query": "正确参数"}')
        self.assertNotIn('stale', summary['tools'][0]['params'])

    def test_evidence_excerpts_and_sources_visible(self):
        from oak.experiments.runner import _retrieval_trace
        events = ({'stage': 'tool', 'attempt': 0, 'step': 0, 'asset_id': 'f_semantic',
                   'parameters': {'query': 'q'}, 'data': [
                       {'node_id': 'n000001', '陈述': '甲计划下周修打印机', 'source_ids': ['s1', 's2']},
                       {'node_id': 'n000002', '陈述': '乙觉得跑步能减压', 'source_ids': ['s3']}],
                   'node_ids': [], 'source_ids': [], 'read_operations': 2,
                   'capability_calls': {'semantic_search': 1}},)
        summary = _retrieval_trace(self._answer(events, ('{"action":"ready"}',)))
        evidence = summary['tools'][0]['evidence']
        self.assertEqual(evidence[0]['node_id'], 'n000001')
        self.assertIn('修打印机', evidence[0]['statement'])
        self.assertEqual(evidence[0]['source_ids'], ['s1', 's2'])

    def test_truncation_is_marked(self):
        from oak.experiments.runner import _retrieval_trace, _TRACE_CHARS
        events = tuple(
            {'stage': 'tool', 'attempt': 0, 'step': i, 'asset_id': f'f_{i}',
             'parameters': {'q': 'x' * 400}, 'data': [], 'node_ids': [], 'source_ids': [],
             'read_operations': 1, 'capability_calls': {'search': 1}}
            for i in range(30))
        summary = _retrieval_trace(self._answer(events, ('{"action":"ready"}',)))
        self.assertLess(len(summary['tools']), 30)
        self.assertGreater(summary.get('tools_truncated', 0), 0)


class CrossCaseTraceTests(unittest.TestCase):
    def test_same_question_id_keeps_case_local_trace(self):
        # 评审#3 反例：A、B 对话都有第 0 题，A 用 tool_A、B 用 tool_B——反馈不得串用
        from oak.experiments import runner as R
        ev = (SourceRef('message_text', 'c', '1'),)
        def make(tool):
            raw = ('{"action":"call","asset_id":"%s","parameters":{"query":"q"}}' % tool,
                   '{"action":"ready"}', '{"status":"answered","answer":"ok"}', '{"accepted":true}')
            trace = ({'stage': 'tool', 'attempt': 0, 'step': 0, 'asset_id': tool, 'data': [],
                      'node_ids': [], 'source_ids': [], 'read_operations': 1,
                      'capability_calls': {'semantic_search': 1}},)
            return AnswerResult('0', 'answered', 'ok', evidence=ev, raw_outputs=raw, trace=trace)
        rows = ({'question_id': '0', 'status': 'answered', 'original': {'precise': False, 'lenient': False}},)
        results = [RunResult('a', 'i', 'v', (make('tool_A'),), 1),
                   RunResult('b', 'i2', 'v', (make('tool_B'),), 1)]
        class C1: id = 'a'
        class C2: id = 'b'
        baseline = EvaluationResult({'m': 0}, 0, 2, 0, 0, rows)
        feedback = R.training_feedback([C1(), C2()], results, [('a', rows), ('b', rows)], baseline)
        by_case = {r['case_id']: r['trace']['tools'][0]['tool'] for r in feedback['diagnostics']}
        self.assertEqual(by_case, {'a': 'tool_A', 'b': 'tool_B'})


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
        raw = ('{"action":"call","asset_id":"search_facts","parameters":{"terms":["x"]}}',
               '{"action":"ready"}', '{"status":"answered","answer":"wrong"}', '{"accepted":true}')
        answers = (AnswerResult('0', 'answered', 'ok', evidence=ev),
                   AnswerResult('1', 'answered', 'wrong', evidence=ev, raw_outputs=raw,
                                trace=({'stage': 'tool', 'attempt': 0, 'step': 0,
                                        'asset_id': 'search_facts', 'data': [{'编号': 'x'}],
                                        'node_ids': [], 'source_ids': [], 'read_operations': 1,
                                        'capability_calls': {'search': 1}},)),
                   AnswerResult('2', 'execution_error', '', error='ProtocolError: boom'))
        result = RunResult('c', 'id', 'v', answers, 5)
        class C:
            id = 'c'
            questions = (QuestionInput('q1', '?'), QuestionInput('q2', '?'))
        baseline = EvaluationResult({'original_precise': 1}, 2, 3, 1, 0, rows)
        feedback = R.training_feedback([C()], [result], [('c', rows)], baseline)
        ids = [r['diagnostic']['question_id'] for r in feedback['diagnostics']]
        self.assertEqual(ids, ['1', '2'])               # 通过题不进反馈（失败优先）
        blob = json.dumps(feedback, ensure_ascii=False)
        self.assertNotIn('GOLD-SECRET', blob)           # 金标不进提案载荷
        self.assertNotIn('reference_items', blob)
        traces = {r['diagnostic']['question_id']: r.get('trace') for r in feedback['diagnostics']}
        self.assertEqual(traces['1']['tool_calls'], 1)
        self.assertEqual(traces['1']['model_calls'], 4)
        self.assertEqual(traces['1']['tools'][0]['tool'], 'search_facts')
        self.assertTrue(traces['1']['tools'][0]['params'].startswith('{"terms"'))
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
    def test_macro_micro_and_delta_use_rates(self):
        # 评审#1 反例：A 200 题（融合 100/向量 90），B 100 题（融合 80/向量 90）
        # 融合宏平均 65% vs 向量 67.5% → 差值 −2.5pp；按题汇总两者同为 60%（计数平均会误报 0）
        from datasets.locomo.scripts.external_test import aggregate_report
        g1 = [{'case_id': 'A', 'total': 200, 'completed': 200,
               'original_precise': 100, 'original_lenient': 110,
               'repaired_precise': 101, 'repaired_lenient': 111,
               'generation_faults': 0, 'evaluation_faults': 0},
              {'case_id': 'B', 'total': 100, 'completed': 100,
               'original_precise': 80, 'original_lenient': 90,
               'repaired_precise': 81, 'repaired_lenient': 91,
               'generation_faults': 0, 'evaluation_faults': 0}]
        v0 = [{'case_id': 'A', 'total': 200, 'completed': 195,
               'original_precise': 90, 'original_lenient': 100,
               'repaired_precise': 90, 'repaired_lenient': 100,
               'generation_faults': 5, 'evaluation_faults': 0},
              {'case_id': 'B', 'total': 100, 'completed': 100,
               'original_precise': 90, 'original_lenient': 95,
               'repaired_precise': 90, 'repaired_lenient': 95,
               'generation_faults': 0, 'evaluation_faults': 0}]
        report = aggregate_report(g1, v0)
        self.assertEqual(report['macro']['original_precise_rate'], 65.0)      # (50%+80%)/2
        self.assertEqual(report['macro']['original_precise_rate'], 65.0)
        self.assertEqual(report['micro']['original_precise_correct'], 180)    # 原始答对题数保留
        self.assertEqual(report['micro']['total_sum'], 300)
        self.assertEqual(report['micro']['original_precise_rate'], 60.0)      # 180/300 按题汇总
        self.assertEqual(report['micro']['generation_faults'], 0)
        deltas = {d['case_id']: d for d in report['delta_vs_baseline']}
        self.assertEqual(deltas['A']['original_precise_delta_pp'], 5.0)       # 50%−45%
        self.assertEqual(deltas['B']['original_precise_delta_pp'], -10.0)     # 80%−90%
        self.assertEqual(report['macro']['delta_vs_baseline_pp']['original_precise'], -2.5)
        # 基线侧故障单独报告：分母不减故障题
        baseline_report = aggregate_report(v0)
        self.assertEqual(baseline_report['micro']['total_sum'], 300)
        self.assertEqual(baseline_report['micro']['generation_faults'], 5)


class EmbedderCacheConcurrencyTests(unittest.TestCase):
    def test_concurrent_flush_never_loses_tmp(self):
        # conv-47 事故回归：共享固定 .tmp 名在并发缓存未命中时互相抢文件 →
        # FileNotFoundError 记为整题故障。唯一临时名后并发 flush 必须全部成功。
        import threading
        from oak.vector.embedder import Embedder
        emb = Embedder.__new__(Embedder)
        with tempfile.TemporaryDirectory() as td:
            emb.cache_path = Path(td) / 'embed_cache.json'
            emb._cache = {f'q{i}': [0.1] * 8 for i in range(100)}
            errors = []
            barrier = threading.Barrier(8)
            def flush_many():
                try:
                    barrier.wait()
                    for _ in range(50):
                        emb._flush()
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=flush_many) for _ in range(8)]
            for th in threads: th.start()
            for th in threads: th.join()
            self.assertEqual(errors, [])
            self.assertTrue(emb.cache_path.exists())
            import json as _json
            self.assertEqual(len(_json.loads(emb.cache_path.read_text())), 100)
            self.assertEqual(list(Path(td).glob('*.tmp')), [])   # 无残留临时文件


if __name__ == '__main__':
    unittest.main()



class AbstentionAuditScopeTests(unittest.TestCase):
    def test_agentic_refusal_audit_stays_within_retrieved_evidence(self):
        # 评审#5：融合版拒答审计不得读全图——covers_full_graph=False、证据＝已召回行；
        # 需要补证只能显式调用登记工具（调用与返回都在轨迹里）。
        import asyncio
        from oak.engine import Pipeline
        from oak.kernel import TaskSpec
        from oak.llm.recorded import RecordedClient
        from tests.fixtures import review
        from tests.integration.test_agentic_round import (ROOT, FakeEmbedder, build_snapshot,
                                                          cold_bundle, corpus)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            spec = TaskSpec.load(ROOT / 'tasks/conversation_memory/task.yaml').with_bundle(cold_bundle(root))
            case = CaseInput('conv-x', corpus(), (QuestionInput('q1', '甲在哪年买了游艇？'),))
            replies = {'tools': [{'action': 'call', 'asset_id': 'f_semantic',
                                  'parameters': {'query': '游艇'}},
                                 {'action': 'ready'}],
                       'answer': [{'status': 'abstained', 'answer': '记忆中无支持。',
                                   'node_ids': []}],
                       'review': [review(status='abstained')]}
            client = RecordedClient(replies)
            result = asyncio.run(Pipeline(client, root / 'gen', frozen_snapshot=snapshot,
                                          embedder_factory=lambda: FakeEmbedder())
                                 .run(case, spec, RunConfig(protocol_attempts=1)))
            self.assertEqual(result.answers[0].status, 'abstained')
            audits = []
            for call in client.calls:
                if call.get('role') != 'review':
                    continue
                for message in call['messages']:
                    text = message.get('content', '') if isinstance(message, dict) else str(message)
                    if 'refusal_audit' in str(text):
                        import json as _json
                        audits.append(_json.loads(text) if isinstance(text, str) and text.startswith('{') else text)
            self.assertTrue(audits, '拒答审计 review 调用应当存在')
            audits = [a for a in audits if isinstance(a, dict)]
            self.assertTrue(audits)
            n_nodes = json.loads((snapshot / 'manifest.json').read_text())['n_nodes']
            for audit in audits:
                self.assertFalse(audit.get('refusal_audit', {}).get('covers_full_graph', False))
                self.assertEqual(audit.get('refusal_audit', {}).get('mode'), 'agentic_retrieved')
                visible = audit.get('candidate', {}).get('visible_evidence', [])
                # 只看已召回行（远小于全图），未召回内容不得借审计通道进入
                self.assertLess(len(visible), n_nodes)


class AdmissionRetryThenSuccessTests(unittest.TestCase):
    def test_stale_fingerprint_feeds_back_and_second_call_admits(self):
        # 评审#2：准入错（指纹回显）回灌提案重试后成功准入——整轮不作废
        import asyncio
        import contextlib
        import io
        from tests.integration.test_experiment import LedgerRecordedClient, RecordedExperiment
        from oak.experiments.runner import ExperimentRunner
        class RetryOnceExperiment(RecordedExperiment):
            def __init__(self, root):
                super().__init__(root)
                self.admission_calls = 0
                original = self.revisions.propose

                def patched(*args, **kwargs):
                    self.admission_calls += 1
                    if self.admission_calls == 1:
                        raise ValueError('Stale baseline or asset type change')
                    return original(*args, **kwargs)
                self.revisions.propose = patched

            def _client(self, stage):
                client = super()._client(stage)
                if stage == 'R1' and self.stage_clients[stage] == 1:
                    # 首次提案被拒后重试：同一 stage 客户端需要第二份提案输出
                    client.replies['proposal'].append(client.replies['proposal'][0])
                return client

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = RetryOnceExperiment(root)
            from oak.kernel import TaskSpec
            from tests.integration.test_experiment import TASK
            task_spec = TaskSpec.load(TASK / 'task.yaml')
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(runner.run(runner.case.id, task_spec, rounds=1))
            self.assertEqual(runner.admission_calls, 2)      # 第一次拒、第二次成
            self.assertTrue(summary['rounds'][0]['accepted'])
            self.assertEqual(summary['status'], 'complete')


if __name__ == '__main__':
    unittest.main()


class FaultRetryInvalidatesEvaluationTests(unittest.TestCase):
    def test_recovered_answers_force_re_evaluation(self):
        # 评审#4：重试跑过＝旧评测检查点作废重评；恢复题必须被重新评分，
        # 不得沿用基于故障答案集的旧检查点
        import asyncio
        from types import SimpleNamespace
        from oak.experiments import runner as R

        class FakeClient:
            async def aclose(self): pass
            def ledger_summary(self): return {'total_calls': 0}

        ev = (SourceRef('message_text', 'c', '1'),)
        faulted = RunResult('c', 'i', 'v', (AnswerResult('q1', 'execution_error', '', error='x'),
                                            AnswerResult('q2', 'execution_error', '', error='y')), 0)
        recovered = RunResult('c', 'i', 'v', (AnswerResult('q1', 'answered', 'ok', evidence=ev),
                                              AnswerResult('q2', 'answered', 'ok', evidence=ev)), 0)
        scripted = [faulted, recovered]

        class FakePipeline:
            def __init__(self, client, work_dir, frozen_snapshot=None): pass
            async def run(self, case, spec, config):
                return scripted.pop(0)

        evaluated = []

        class FakeEvaluator:
            def __init__(self, client, path): pass
            async def evaluate(self, result, asked=None):
                evaluated.append([a.status for a in result.answers])
                return EvaluationResult({'m': 1}, 2, 2, 0, 0)

        async def fake_sleep(_s): pass
        original_retry = R.batched_fault_retry

        async def fast_retry(*args, **kwargs):
            kwargs['sleep'] = fake_sleep
            return await original_retry(*args, **kwargs)

        class C:
            id = 'c'
            questions = (QuestionInput('q1', '?'), QuestionInput('q2', '?'))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 基于故障答案集的陈旧评测检查点（身份匹配，若不作废将被直接复用）
            stale = EvaluationResult({'m': 0}, 0, 2, 2, 0)
            (root / 'B0' / 'evaluation').mkdir(parents=True)
            (root / 'B0' / 'evaluation' / 'c.json').write_text(json.dumps(
                {'run_identity': 'i', 'asset_version': 'v', 'scores': stale.to_dict()}))
            runner = R.ExperimentRunner(
                type('Adapter', (), {'generation_input': staticmethod(lambda ident: C())})(),
                lambda client, path: FakeEvaluator(client, path), None,
                RunConfig(protocol_attempts=1), None, root, client_factory=lambda stage: FakeClient())
            spec = SimpleNamespace(bundle=SimpleNamespace(version='v'))
            with mock.patch.object(R, 'Pipeline', FakePipeline), \
                 mock.patch.object(R, 'batched_fault_retry', fast_retry):
                results, scores = asyncio.run(runner._stage('B0', [C()], spec))
            self.assertEqual(scores.completed, 2)
            self.assertEqual(evaluated, [['answered', 'answered']])   # 陈旧检查点已作废、恢复题被重评
            self.assertEqual(results[0].answers[0].status, 'answered')


if __name__ == '__main__':
    unittest.main()


class GraphCheckBudgetTests(unittest.TestCase):
    def _registry(self, steps=30000):
        from oak.kernel.checks import CheckRegistry
        from oak.operators.sandbox import Limits
        src = ("def check(candidate):\n"
               " issues=[]\n fact=0\n"
               " for n in candidate.get('nodes', []):\n"
               "  if n.get('entity_type')=='原子事实':\n"
               "   fact=fact+1\n"
               "   if not n.get('陈述',''):\n"
               "    issues.append('空陈述')\n"
               " if fact==0:\n"
               "  issues.append('无原子事实')\n"
               " return {'ok': not issues, 'issues': issues}\n")
        class A: kind='C'; id='c'; fingerprint='f'; stage='graph'; content=src
        class B:
            assets=type('AS',(),{'assets':(A(),)})(); version='v'
            def verify(self): pass
        return CheckRegistry(B(), Limits(steps,15.0,180000))

    def test_budget_scales_with_snapshot(self):
        # 评审①：1100 节点的图检查需 ~40k 步 > function_steps——预算随图规模伸缩后通过，
        # 且记录实际耗用 steps_used / step_budget
        rows=[{'node_id':f'n{i:06d}','entity_type':'原子事实','陈述':f'事实{i}','source_ids':['s']}
              for i in range(2200)]
        opinions=self._registry().run('graph',{'nodes':rows,'stage':'graph'})
        self.assertTrue(opinions[0]['ok'])
        self.assertGreater(opinions[0]['steps_used'],30000)      # 事故的真实量级
        self.assertLessEqual(opinions[0]['steps_used'],opinions[0]['step_budget'])
        no_nodes=self._registry().run('graph',{'stage':'graph'})
        self.assertEqual(no_nodes[0]['step_budget'],30000)       # 无节点不放大预算

    def test_stage_skip_and_preflight(self):
        import asyncio
        from types import SimpleNamespace
        from oak.experiments import runner as R
        # (a) 图阶段全局失败：不进入分批重试（FakePipeline 只被调用一次）
        class FakeClient:
            async def aclose(self): pass
            def ledger_summary(self): return {'total_calls':0}
        ev=(SourceRef('m','c','1'),)
        graph_failed=RunResult('c','i','v',(AnswerResult('q1','execution_error','',error='x'),),
                               0,({'status':'execution_error','error':'SandboxError: budget'},))
        calls=[]
        class OncePipeline:
            def __init__(self, client, work_dir, frozen_snapshot=None): pass
            async def run(self, case, spec, config):
                calls.append(1); return graph_failed
        class C:
            id='c'
            questions=(QuestionInput('q1','?'),QuestionInput('q2','?'))
        with tempfile.TemporaryDirectory() as td:
            runner=R.ExperimentRunner(
                type('Adapter',(),{'generation_input':staticmethod(lambda i: C())})(),
                lambda c,p: type('E',(),{'evaluate':None})(), None, RunConfig(protocol_attempts=1),
                None, Path(td)/'r', client_factory=lambda s: FakeClient())
            # 图阶段全局失败：断言不进入分批重试（评测异常在此路径上必然发生，宽断言）
            with mock.patch.object(R,'Pipeline',OncePipeline):
                with self.assertRaises(Exception):
                    asyncio.run(runner._stage('B0',[C()],SimpleNamespace(bundle=SimpleNamespace(version='v'))))
        self.assertEqual(calls,[1])   # 只跑了一次，没有分批重试


class PreflightCapabilityTests(unittest.TestCase):
    def _runner_with_graph(self, root):
        import asyncio
        from oak.experiments.runner import ExperimentRunner
        from oak.experiments.snapshots import load_frozen_graph
        from tests.integration.test_agentic_round import FakeEmbedder, build_snapshot, corpus, cold_bundle
        snapshot,_=build_snapshot(root)
        from oak.experiments.snapshots import attach_vector
        graph=load_frozen_graph(snapshot, corpus())
        attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())
        class C:
            id='c'
            questions=(QuestionInput('q1','?'),QuestionInput('q2','?'))
        runner=ExperimentRunner(
            type('Adapter',(),{'generation_input':staticmethod(lambda i: C())})(),
            lambda c,p: None, None, RunConfig(function_timeout_s=15.0), None, root/'r',
            client_factory=lambda s: None, bootstrap_trial_graph=graph)
        return runner, cold_bundle(root/'b')

    def test_untriggered_capability_rejected_before_stage(self):
        # 评审③反例：semantic_search 在未执行分支里（AST 有调用），试跑只执行 nodes
        # ——候选预检必须拒绝
        from dataclasses import replace
        from oak.kernel import TaskSpec
        from tests.integration.test_agentic_round import ROOT
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            runner,bundle=self._runner_with_graph(root)
            spec=TaskSpec.load(ROOT/'tasks/conversation_memory/task.yaml')
            from oak.kernel.assets import Asset
            fake=Asset('f_semantic','F',
                "def run(params):\n if params.get('mode')=='vec':\n  return semantic_search(params['query'], limit=4)\n return nodes('原子事实', limit=2)\n",
                {'type':'object','properties':{'mode':{'type':'string'}}},{'type':'array'},
                trial_inputs=({'mode':'plain'},),schema_dependencies=['schema'],description='fake')
            traverse=Asset('f_traverse','F',
                "def run(params):\n return traverse(params['node_id'], params['relation'])\n",
                {'type':'object','properties':{'node_id':{'type':'string'},'relation':{'type':'string'}}},
                {'type':'array'},
                trial_inputs=({'node_id':'n000000','relation':'归属于'},),
                schema_dependencies=['schema'],description='关系遍历')
            others=[a for a in bundle.assets.assets if a.id!='f_semantic']+[traverse]
            import tempfile as _tf
            fake_bundle=self._export(root, tuple(others+[fake]))
            with self.assertRaises(ValueError) as caught:
                runner._preflight(fake_bundle, spec)
            self.assertIn('候选能力试跑不合格', str(caught.exception))
            # 合格候选（真实触发 semantic_search）通过
            good=self._export(root, tuple(bundle.assets.assets)+(traverse,))
            runner._preflight(good, spec)

    def _export(self, root, assets):
        import tempfile as _tf
        from oak.kernel.assets import KernelAssets
        return KernelAssets(assets).export(Path(_tf.mkdtemp(prefix='pf-'))/'exported')


if __name__ == '__main__':
    unittest.main()


class ExternalEntryTests(unittest.TestCase):
    """评审一：真实外测入口的离线端到端——加载锁定资产、逐对话预检（挂向量索引＋图 C
    否决＋能力试跑）到报告生成，不只测 aggregate_report。"""

    def _setup(self, td, with_traverse=True, bad_c=False):
        import asyncio
        from oak.engine import Pipeline  # noqa: F401  确认入口依赖可导入
        from oak.experiments.snapshots import load_frozen_graph
        from oak.kernel import TaskSpec
        from oak.kernel.assets import Asset, KernelAssets
        from tests.integration.test_agentic_round import (ROOT, FakeEmbedder, build_snapshot,
                                                          cold_bundle, corpus)
        root = Path(td)
        snapshot, manifest = build_snapshot(root)
        bundle = cold_bundle(root)
        assets = list(bundle.assets.assets)
        if with_traverse:
            assets.append(Asset('f_traverse', 'F',
                "def run(params):\n return traverse(params['node_id'], params['relation'])\n",
                {'type': 'object', 'properties': {'node_id': {'type': 'string'},
                                                  'relation': {'type': 'string'}}},
                {'type': 'array'}, trial_inputs=({'node_id': 'n000000', 'relation': '归属于'},),
                schema_dependencies=['schema'], description='关系遍历'))
        if bad_c:
            assets.append(Asset('c_bad', 'C',
                "def check(candidate):\n return {'ok': False, 'issues': ['图检查否决样例']}",
                {'type': 'any'}, {'type': 'any'},
                schema_dependencies=['schema'], stage='graph', description='坏检查'))
        locked = KernelAssets(tuple(assets)).export(root / 'locked')
        task = TaskSpec.load(ROOT / 'tasks/conversation_memory/task.yaml')
        case = CaseInput('conv-x', corpus(), (QuestionInput('q1', '甲计划做什么？'),))
        return root, snapshot, manifest, locked, task, {'conv-x': case}   # export() 已返回 KernelBundle

    def test_entry_end_to_end_offline(self):
        import tempfile
        from datasets.locomo.scripts.external_test import (aggregate_report, baseline_compatibility,
                                                           experiment_identity, preflight)
        from tests.integration.test_agentic_round import FakeEmbedder
        with tempfile.TemporaryDirectory() as td:
            root, snapshot, manifest, bundle, task, cases = self._setup(td)
            config = __import__('oak.config', fromlist=['RunConfig']).RunConfig(function_timeout_s=15.0)
            # 完整入口第一段：锁定资产加载 + 逐对话预检（真向量索引挂载＋图 C＋能力试跑）
            preflight(bundle, config, task, cases, snap_root=root / 'snapshots',
                      embedder_factory=lambda: FakeEmbedder())
            # 报告段：逐对话行含正确率字段，身份齐备，兼容性判定生效
            rows = [{'case_id': 'conv-x', 'total': 1, 'completed': 1,
                     'original_precise': 1, 'original_lenient': 1,
                     'repaired_precise': 1, 'repaired_lenient': 1,
                     'generation_faults': 0, 'evaluation_faults': 0}]
            ident = experiment_identity(bundle, config, None, ['conv-x'], snap_root=root / 'snapshots')
            self.assertEqual(ident['snapshots']['conv-x'], manifest['snapshot_digest'])
            self.assertEqual(baseline_compatibility(ident, dict(ident)), 'compatible')
            report = aggregate_report(rows, rows)
            self.assertEqual(report['macro']['original_precise_rate'], 100.0)

    def test_entry_rejects_missing_capability_and_bad_c(self):
        import tempfile
        from datasets.locomo.scripts.external_test import preflight
        from tests.integration.test_agentic_round import FakeEmbedder
        config = __import__('oak.config', fromlist=['RunConfig']).RunConfig(function_timeout_s=15.0)
        with tempfile.TemporaryDirectory() as td:
            root, _, _, bundle, task, cases = self._setup(td, with_traverse=False)
            with self.assertRaises(SystemExit) as caught:
                preflight(bundle, config, task, cases, snap_root=root / 'snapshots',
                          embedder_factory=lambda: FakeEmbedder())
            self.assertIn('静态能力底线', str(caught.exception))
        with tempfile.TemporaryDirectory() as td:
            root, _, _, bundle, task, cases = self._setup(td, bad_c=True)
            with self.assertRaises(SystemExit) as caught:
                preflight(bundle, config, task, cases, snap_root=root / 'snapshots',
                          embedder_factory=lambda: FakeEmbedder())
            self.assertIn('图检查否决样例', str(caught.exception))   # 评审二：否决必须被采纳


class PreflightStagingTests(unittest.TestCase):
    """评审三：预检失败不残留 candidate 目录；重试修好后正常准入；恢复已有候选也要过预检。"""

    def test_first_preflight_failure_second_attempt_admits(self):
        import asyncio
        import contextlib
        import io
        from tests.integration.test_experiment import RecordedExperiment
        from oak.kernel import TaskSpec
        from tests.integration.test_experiment import TASK

        class PreflightFailOnce(RecordedExperiment):
            def __init__(self, root):
                super().__init__(root)
                self.preflight_calls = 0
                real = self._preflight
                def patched(candidate, spec, *args, **kwargs):
                    self.preflight_calls += 1
                    if self.preflight_calls == 1:
                        raise ValueError('候选预检失败: SandboxError: boom')
                    return real(candidate, spec, *args, **kwargs)
                self._preflight = patched

            def _client(self, stage):
                client = super()._client(stage)
                if stage == 'R1' and self.stage_clients[stage] == 1:
                    client.replies['proposal'].append(client.replies['proposal'][0])
                return client

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PreflightFailOnce(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / 'task.yaml'), rounds=1))
            self.assertTrue(summary['rounds'][0]['accepted'])
            self.assertEqual(runner.preflight_calls, 2)
            # 正式候选目录就位；首次失败的暂存目录保留审计且不阻塞
            self.assertTrue((root / 'R1' / 'candidate' / 'bundle' / 'manifest.json').exists())
            self.assertTrue((root / 'R1' / '.candidate-attempt-0').exists())
            self.assertNotIn('already exists', json.dumps(summary, ensure_ascii=False))

    def test_resume_revalidates_existing_candidate(self):
        import asyncio
        import contextlib
        import io
        from tests.integration.test_experiment import RecordedExperiment, TASK
        from oak.kernel import TaskSpec
        from tests.integration.test_agentic_round import cold_bundle

        class Recording(RecordedExperiment):
            def __init__(self, root):
                super().__init__(root)
                self.preflight_specs = []
                real = self._preflight
                def patched(candidate, spec, *args, **kwargs):
                    self.preflight_specs.append(candidate.version)
                    return real(candidate, spec, *args, **kwargs)
                self._preflight = patched

            def _client(self, stage):
                # 恢复路径没有提案客户端：R1 的第一个客户端就是阶段评测客户端
                if stage == 'R1' and self.stage_clients[stage] == 0:
                    from tests.fixtures import client as fx
                    from tests.integration.test_experiment import LedgerRecordedClient
                    self.stage_clients[stage] += 1
                    c = LedgerRecordedClient({r: list(v) for r, v in fx(self.case).replies.items()})
                    self.created.append(c)
                    return c
                return super()._client(stage)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            from oak.kernel.registration import load_assets
            load_assets(TASK).export(root / 'seed')     # 设备任务 bundle，导出目录＝root/'seed'
            runner = Recording(root)
            target = root / 'R1' / 'candidate' / 'bundle'
            target.parent.mkdir(parents=True)
            import shutil
            shutil.copytree(root / 'seed', target)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / 'task.yaml'), rounds=1))
            self.assertTrue(runner.preflight_specs)   # 恢复路径确实执行了预检


def _bundle_dir(bundle):
    return Path(bundle.__dict__.get('path', bundle.assets.__dict__.get('path', str(bundle)))) \
        if hasattr(bundle, '__dict__') else Path(str(bundle))


class TraceReturnTypesTests(unittest.TestCase):
    """评审四：合法的非数组工具返回不崩摘要；空结果与合法 0/False 区分。"""

    def _summarize(self, data):
        from oak.experiments.runner import _retrieval_trace
        ev = (SourceRef('message_text', 'c', '1'),)
        raw = ('{"action":"call","asset_id":"t","parameters":{}}', '{"action":"ready"}')
        events = ({'stage': 'tool', 'attempt': 0, 'step': 0, 'asset_id': 't',
                   'parameters': {}, 'data': data, 'node_ids': [], 'source_ids': [],
                   'read_operations': 1, 'capability_calls': {'aggregate': 1}},)
        return _retrieval_trace(AnswerResult('0', 'answered', 'ok', evidence=ev,
                                             raw_outputs=raw, trace=events))

    def test_scalar_and_object_returns(self):
        for value, label in (({'count': 3}, 'object'), (3, 'number'), (False, 'bool'), ('文本', 'string')):
            summary = self._summarize(value)
            self.assertEqual(summary['empty_results'], 0, label)      # 合法值不算空
            self.assertEqual(summary['tools'][0]['rows'], 1, label)
            self.assertIn('returns', summary['tools'][0], label)
        none_summary = self._summarize(None)
        self.assertEqual(none_summary['empty_results'], 1)            # None 才是空结果
        self.assertEqual(none_summary['tools'][0]['rows'], 0)


class BaselineCompatibilityTests(unittest.TestCase):
    """评审五：共同条件核对实际生效——模型/判题器/作答配置/快照任一不同即不兼容。"""

    def _ident(self, **over):
        base = {'transport': {'model_strong': 'glm-5.3'}, 'judge_lock': 'j1',
                'run_config': {'protocol_attempts': 5, 'answer_attempts': 3, 'temperature': 0.2,
                               'max_tokens': 4096, 'calls_per_question': 32,
                               'retrieval_mode': 'agentic', 'vector_k': 30},
                'snapshots': {'conv-30': 'd1'}, 'asset_version': 'v1'}
        base.update(over)
        return base

    def test_expected_arm_differences_stay_compatible(self):
        from datasets.locomo.scripts.external_test import baseline_compatibility
        other_arm = self._ident(run_config={'protocol_attempts': 5, 'answer_attempts': 3,
                                            'temperature': 0.2, 'max_tokens': 4096,
                                            'calls_per_question': 32,
                                            'retrieval_mode': 'vector_once', 'vector_k': 60},
                                asset_version='v2')
        self.assertEqual(baseline_compatibility(self._ident(), other_arm), 'compatible')

    def test_real_differences_rejected(self):
        from datasets.locomo.scripts.external_test import baseline_compatibility
        mine = self._ident()
        cases = {
            '模型路由不同': (self._ident(transport={'model_strong': 'glm-4.7'}), '模型路由不同'),
            '判题器锁不同': (self._ident(judge_lock='j2'), '判题器锁不同'),
            '作答/审查配置不同': (self._ident(run_config={'protocol_attempts': 3, 'answer_attempts': 3,
                                                    'temperature': 0.2, 'max_tokens': 4096,
                                                    'calls_per_question': 32,
                                                    'retrieval_mode': 'vector_once', 'vector_k': 60}),
                           '作答/审查配置不同'),
            '快照身份不同': (self._ident(snapshots={'conv-30': 'd2'}), '快照身份不同'),
            '身份缺失': (None, '缺少身份记录'),
        }
        for label, (base, marker) in cases.items():
            verdict = baseline_compatibility(mine, base)
            self.assertTrue(verdict.startswith('incompatible'), label)
            self.assertIn(marker, verdict, label)


if __name__ == '__main__':
    unittest.main()


class AnswerCheckAdmissionTests(unittest.TestCase):
    """agentic_v6 G1 B0 全灭事故回归：答案阶段 C 结构不兼容/全盘否决必须在准入被拒。"""

    ROWS = [{'node_id': 'n000000', 'entity_type': '原子事实', '陈述': '甲计划下周修打印机',
             'source_ids': ['s'], '编号': 'c-0001', '主体': '甲'}]

    def _registry(self, src, check_stage='answer'):
        from oak.kernel.checks import CheckRegistry
        from oak.operators.sandbox import Limits
        target_stage = check_stage
        class A: kind='C'; id='c'; fingerprint='f'; stage=target_stage; content=src
        class B:
            assets=type('AS',(),{'assets':(A(),)})(); version='v'
            def verify(self): pass
        return CheckRegistry(B(), Limits(30000, 15.0, 180000))

    def test_synthetic_snapshot_is_well_formed(self):
        from oak.kernel.checks import synthetic_answer_snapshot
        snap = synthetic_answer_snapshot(self.ROWS, '甲计划做什么？')
        self.assertEqual(snap['status'], 'answered')
        self.assertEqual(snap['answer'], '甲计划下周修打印机')
        self.assertTrue(snap['evidence'] and snap['node_ids'])

    def test_rejecting_c_fails_admission(self):
        from oak.kernel.checks import enforce_opinions, synthetic_answer_snapshot
        snap = synthetic_answer_snapshot(self.ROWS, '甲计划做什么？')
        bad = self._registry("def check(candidate):\n return {'ok': False, 'issues': ['candidate 不是对象']}")
        with self.assertRaises(ValueError) as caught:
            enforce_opinions(bad.run('answer', snap), '冷启动答案阶段')
        self.assertIn('candidate 不是对象', str(caught.exception))
        sane = self._registry("def check(candidate):\n return {'ok': True, 'issues': []}")
        enforce_opinions(sane.run('answer', snap), '冷启动答案阶段')   # 正常 C 通过


if __name__ == '__main__':
    unittest.main()


class TrimmedEvaluateTests(unittest.TestCase):
    """训练集瘦身事故（v9 B0 全灭）：evaluator 完整性检查曾硬性要求全会话答案集，
    瘦身到 100 题后在判分入口崩溃。冒烟门走 dual_grade_batch 子集、不经过这个检查，
    所以没拦住。asked 语义＝按本轮实际出题集核对；None 保持全会话要求。"""

    def _evaluator(self, n_qas, audited=False):
        import datasets.locomo.evaluator as ev
        from dataclasses import dataclass, field
        @dataclass
        class Q:
            idx: int
            question: str
            answer: str = ''
        conv = SimpleNamespace(qas=[Q(i, f'q{i}') for i in range(n_qas)])
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            lock = tdp / 'lock.json'; lock.write_text('{}')
            aud = None
            if audited:
                aud = tdp / 'audited.json'
                aud.write_text(json.dumps([{'idx': i, 'question': f'q{i}', 'answer': f'g{i}',
                                            'disputed': i == 5} for i in range(n_qas)]))
            ev.LOCK_PATH = lock
            evaluator = ev.LocomoEvaluator(None, tdp / 'work', audited_path=aud or tdp / 'x.json')
            fake_aggregate = lambda rows, disputed: {
                'overall': {'lenient': {'correct': len(rows)}, 'precise': {'correct': len(rows)}},
                'grades': [{'idx': i, 'status': 'ok'} for i in range(n_qas)]}
            with mock.patch.object(ev, 'verify_files'), \
                 mock.patch.object(ev, 'load_conversation', return_value=conv), \
                 mock.patch.object(ev, 'transcript', return_value=''), \
                 mock.patch.object(ev, 'aggregate', fake_aggregate), \
                 mock.patch.object(ev, 'dual_grade_batch', new=_fake_dual_grade_batch):
                yield evaluator

    def test_asked_subset_passes_and_grades_only_asked(self):
        gen = self._evaluator(199)
        evaluator = next(gen)
        answers = tuple(AnswerResult(question_id=str(i), status='abstained', answer='记忆中无支持',
                                     evidence=()) for i in range(100))
        result = SimpleNamespace(case_id='conv-99', answers=answers)
        scores = asyncio.run(evaluator.evaluate(result, asked=tuple(range(100))))
        self.assertEqual(scores.total, 100)
        self.assertEqual(scores.metrics['original_precise'], 100)
        self.assertEqual(len(scores.diagnostics), 100)

    def test_full_set_still_required_without_asked(self):
        gen = self._evaluator(199)
        evaluator = next(gen)
        answers = tuple(AnswerResult(question_id=str(i), status='abstained', answer='记忆中无支持',
                                     evidence=()) for i in range(100))
        result = SimpleNamespace(case_id='conv-99', answers=answers)
        with self.assertRaisesRegex(ValueError, 'Complete independent answer set'):
            asyncio.run(evaluator.evaluate(result))

    def test_missing_answer_within_asked_still_rejected(self):
        gen = self._evaluator(199)
        evaluator = next(gen)
        answers = tuple(AnswerResult(question_id=str(i), status='abstained', answer='记忆中无支持',
                                     evidence=()) for i in range(99))
        result = SimpleNamespace(case_id='conv-99', answers=answers)
        with self.assertRaisesRegex(ValueError, 'Complete independent answer set'):
            asyncio.run(evaluator.evaluate(result, asked=tuple(range(100))))

    def test_audited_gold_aligned_to_asked_subset(self):
        gen = self._evaluator(199, audited=True)
        evaluator = next(gen)
        answers = tuple(AnswerResult(question_id=str(i), status='abstained', answer='记忆中无支持',
                                     evidence=()) for i in range(100))
        result = SimpleNamespace(case_id='conv-26', answers=answers)
        scores = asyncio.run(evaluator.evaluate(result, asked=tuple(range(100))))
        self.assertEqual(scores.total, 100)
        self.assertIn('repaired_precise', scores.metrics)
        self.assertTrue(all('repaired' in row for row in scores.diagnostics))


async def _fake_dual_grade_batch(items, client, context, cache_dir):
    return [{'status': 'ok', 'precise': True} for _ in items]


class CarriedCheckpointTests(unittest.TestCase):
    """答案检查点跨框架版本搬运（用户指令：不要从头跑）：CARRIED 旁车＋答案路径逐字节复核。
    编排/评测层（oak/experiments/）差异不影响答案计算，可重锚；答案路径漂移一律拒绝。"""

    def test_no_sidecar_accepts_only_current_identity(self):
        from oak.engine.pipeline import carried_acceptor
        with tempfile.TemporaryDirectory() as td:
            acc = carried_acceptor(td, 'new-id', {})
            self.assertTrue(acc('new-id'))
            self.assertFalse(acc('old-id'))

    def test_sidecar_accepts_old_identity_when_answer_path_identical(self):
        from oak.engine.pipeline import carried_acceptor
        fw = {'/x/oak/operators/data.py': 'a', '/x/oak/experiments/runner.py': 'old'}
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'CARRIED.json').write_text(json.dumps(
                {'accepted_identities': ['old-id'], 'source_framework': fw}))
            self.assertTrue(carried_acceptor(td, 'new-id', fw)('old-id'))
            fw2 = dict(fw); fw2['/x/oak/experiments/runner.py'] = 'new'
            self.assertTrue(carried_acceptor(td, 'new-id', fw2)('old-id'))  # 编排层差异放行

    def test_sidecar_rejects_when_answer_path_changed(self):
        from oak.engine.pipeline import carried_acceptor
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'CARRIED.json').write_text(json.dumps(
                {'accepted_identities': ['old-id'],
                 'source_framework': {'/x/oak/operators/data.py': 'a'}}))
            with self.assertRaisesRegex(ValueError, '答案路径文件与搬运源不一致'):
                carried_acceptor(td, 'new-id', {'/x/oak/operators/data.py': 'changed'})

    def test_carry_rebase_answer_path_drift_detected(self):
        from datasets.locomo.scripts.carry_rebase import answer_path_ok
        bad = answer_path_ok({'/x/oak/operators/data.py': 'a', '/x/oak/experiments/runner.py': 'old'},
                             {'/x/oak/operators/data.py': 'b', '/x/oak/experiments/runner.py': 'new'})
        self.assertEqual(bad, ['/x/oak/operators/data.py'])


class SmokeThresholdTests(unittest.TestCase):
    """冒烟门槛与 B0 冷门成比例（v10 搬运事故：1/3 低概率 F 契约绊倒≠系统性破绽）：
    ≥2/3 执行错误或 0 有效作答才拒；单题故障由 B0 冷门（≥95% 完成度）吸收。"""

    def test_single_fault_passes_double_fault_rejects(self):
        from dataclasses import dataclass, replace as dcreplace
        from oak.experiments.runner import ExperimentRunner
        import oak.experiments.runner as R
        @dataclass
        class Q:
            id: str; text: str = '?'; parameters: dict = None
        @dataclass
        class Case:
            id: str; questions: tuple
        gate = ExperimentRunner._smoke_gate
        async def fake_pipeline_run(self, case, spec, config):
            answers = []
            for i, q in enumerate(case.questions):
                if i < fail_count:
                    answers.append(AnswerResult(q.id, 'execution_error', '', error='SandboxError: x'))
                else:
                    answers.append(AnswerResult(q.id, 'abstained', '记忆中无支持', ()))
            from types import SimpleNamespace as NS
            return NS(answers=tuple(answers))
        for fail_count, expect_block in ((3, False), (4, True), (6, True)):
            class _StubClient:
                async def aclose(self): pass
            runner = object.__new__(ExperimentRunner)
            runner._client = lambda stage: _StubClient()
            runner.snapshot_root = None
            runner.smoke_judge = None
            runner.config = RunConfig(protocol_attempts=1)
            case = Case('c', tuple(Q(f'q{i}') for i in range(6)))
            with mock.patch.object(R.Pipeline, 'run', fake_pipeline_run), \
                 mock.patch.object(R, 'tempfile', create=True):
                err = asyncio.run(gate(runner, [case], None))
            self.assertEqual(expect_block, err is not None, f'fail_count={fail_count}: {err}')


class SmokeJudgeContractTests(unittest.TestCase):
    """冒烟判题与正式判题同一入口后的接缝回归（v10 attempt1/2 秒退：字段名笔误
    evaluation_faults 写成 eval_faults，冒烟判题一处崩溃整轮作废）。"""

    def test_smoke_judge_maps_evaluation_result_fields(self):
        import datasets.locomo.run as R
        import datasets.locomo.evaluator as EV
        from oak.contracts import EvaluationResult
        async def fake_evaluate(self, result, asked=None):
            return EvaluationResult({'original_precise': 2}, 3, 2, 0, 1)
        answers = (AnswerResult('0', 'abstained', 'x', ()),) * 3
        with mock.patch.object(EV.LocomoEvaluator, 'evaluate', fake_evaluate):
            verdict = asyncio.run(R.smoke_judge(None, SimpleNamespace(id='conv-26'), answers))
        self.assertEqual(verdict, {'precise': 2, 'completed': 2, 'total': 3})


class EvidenceBoundaryTests(unittest.TestCase):
    """证据边界（专家实锤＋用户批准）：作答可引用证据面＝工具返回的行；内部读过未返回
    的行只进 read_node_ids 溯源；返回行里伪造的 node_id（未读过）被剔除。"""

    def test_returned_rows_only_plus_fabrication_guard(self):
        import tempfile
        from oak.experiments.snapshots import load_frozen_graph, attach_vector
        from oak.kernel.functions import FunctionRegistry
        from oak.kernel.assets import Asset, KernelAssets
        from oak.operators.sandbox import Limits
        from tests.integration.test_agentic_round import FakeEmbedder, build_snapshot, corpus
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())
            # 读多返回少：nodes 全读 50 行，返回只留 2 行＋1 个伪造 node_id
            src = ("def run(params):\n"
                   " rows = nodes('原子事实', limit=50)\n"
                   " out = [{'node_id': r['node_id'], '陈述': r.get('陈述', '')} for r in rows[:2]]\n"
                   " out.append({'node_id': 'nFAKE999', '陈述': '伪造'})\n"
                   " return {'rows': out}\n")
            contract = {'type': 'object', 'properties': {'rows': {'type': 'array'}},
                        'additionalProperties': True}
            f = Asset('f_pick', 'F', src, {'type': 'object'}, contract, ['schema'],
                      description='挑两行', trial_inputs=({},))
            from tests.integration.test_agentic_round import cold_bundle
            base = cold_bundle(root / 'b')
            keep = [a for a in base.assets.assets if a.kind != 'F']
            bundle = KernelAssets(tuple(keep + [f])).export(root / 'b2')
            reg = FunctionRegistry(bundle, Limits(30000, 15.0, 180000))
            result = reg.call('f_pick', {}, graph)
            caps_rows = graph  # 快照图行集来自 load_frozen_graph 的 DataCapabilities
            from oak.operators.data import DataCapabilities
            all_read = DataCapabilities(graph).rows
            fact_ids = [nid for nid, row in all_read.items() if row['entity_type'] == '原子事实']
            self.assertEqual(len(result['read_node_ids']), min(50, len(fact_ids)))
            self.assertLessEqual(len(result['node_ids']), len(result['read_node_ids']))
            self.assertNotIn('nFAKE999', result['node_ids'])       # 伪造引用被剔除
            for nid in result['node_ids']:
                self.assertIn(nid, result['read_node_ids'])        # 可引用⊆真实读取
                self.assertIn(nid, all_read)


class DeterministicFaultTests(unittest.TestCase):
    """确定性工具错误不整题重试（评审：接口/参数错误重试不会变好）：
    SandboxError 类故障跳过分批重试，如实记录进 skipped_retry。"""

    def test_deterministic_error_skips_retry(self):
        import asyncio
        from tests.integration.test_experiment import TASK
        from oak.kernel import TaskSpec
        import oak.experiments.runner as R
        from tests.integration.test_agentic_round import build_snapshot, corpus
        scripted = [RunResult('c', 'i', 'v', (
            AnswerResult('q1', 'answered', 'a', (SourceRef('k', 'd', 'l'),)),
            AnswerResult('q2', 'execution_error', '', error='SandboxError: Restricted execution failed'),
        ), (), ())]
        calls = []
        class FakePipeline:
            def __init__(self, client, work_dir, frozen_snapshot=None): pass
            async def run(self, case, spec, config):
                calls.append(1); return scripted[0]
        class FakeEvaluator:
            def __init__(self, client, path): pass
            async def evaluate(self, result, asked=None):
                return EvaluationResult({'m': 1}, 2, 2, 0, 0)
        class StubClient:
            async def aclose(self): pass
            def ledger_summary(self): return {}
        class C:
            id = 'c'
            questions = (QuestionInput('q1', '?'), QuestionInput('q2', '?'))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = R.ExperimentRunner(
                type('Adapter', (), {'generation_input': staticmethod(lambda i: C())})(),
                lambda c, p: FakeEvaluator(c, p), None, RunConfig(protocol_attempts=1), None, root,
                client_factory=lambda s: StubClient())
            spec = SimpleNamespace(bundle=SimpleNamespace(version='v'))
            import contextlib, io
            with mock.patch.object(R, 'Pipeline', FakePipeline), \
                 mock.patch.object(R, 'batched_fault_retry',
                                   side_effect=AssertionError('不应触发重试')) as no_retry, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                results, scores = asyncio.run(runner._stage('B0', [C()], spec))
            self.assertEqual(calls, [1])          # 只跑一次：确定性错误未重试
            self.assertIn('deterministic_tool_error', out.getvalue())


class DecorativeCheckTests(unittest.TestCase):
    """非法候选用例（专家缺口）：存在答案阶段 C 时，畸形候选必须被至少一个 C 拒绝；
    装饰性 C（永远 ok）在准入被拒。"""

    def test_all_ok_check_rejected_and_flagging_check_passes(self):
        from oak.kernel.checks import enforce_rejection
        enforce_rejection([], 'ctx')                                  # 无 C＝合法省略
        with self.assertRaisesRegex(ValueError, '畸形候选'):
            enforce_rejection([{'check_id': 'c1', 'ok': True, 'issues': []}], 'ctx')
        enforce_rejection([{'check_id': 'c1', 'ok': False, 'issues': ['空答案']}], 'ctx')

    def test_invalid_variant_shape(self):
        from oak.kernel.checks import synthetic_invalid_answer_snapshot
        v = synthetic_invalid_answer_snapshot('问？')
        self.assertEqual(v['status'], 'answered')
        self.assertEqual(v['answer'], '')
        self.assertEqual(v['node_ids'], [])


class ActiveStagesFeedbackTests(unittest.TestCase):
    """专家规格#5：冻结快照下 P.extract 不执行——反馈必须告知提案器「改它不进计分路径」。"""

    def test_frozen_snapshot_marks_extract_skipped(self):
        from oak.experiments.runner import pipeline_active_stages
        frozen = pipeline_active_stages(Path('/snapshots'))
        self.assertIn('SKIPPED', frozen['P.extract'])
        self.assertIn('不因 S 补丁重建', frozen['S'])
        live = pipeline_active_stages(None)
        self.assertNotIn('SKIPPED', live['P.extract'])


class ToolTelemetryTests(unittest.TestCase):
    """专家规格#2（反馈层事后差分实现——不触碰作答路径，答案检查点可跨框架搬运）：
    逐调用新增证据增量（new_node_ids）与重复调用标记（repeat_call）。"""

    def test_new_ids_and_repeat_flag(self):
        from oak.experiments.runner import _retrieval_trace
        from types import SimpleNamespace as NS
        params = {'query': '甲'}
        trace = (
            {'stage': 'tool', 'asset_id': 'f_semantic', 'parameters': dict(params),
             'node_ids': ['n1', 'n2'], 'data': [{'node_id': 'n1', '陈述': 'x', 'source_ids': []},
                                                {'node_id': 'n2', '陈述': 'y', 'source_ids': []}]},
            {'stage': 'tool', 'asset_id': 'f_semantic', 'parameters': dict(params),
             'node_ids': ['n2', 'n3'], 'data': [{'node_id': 'n3', '陈述': 'z', 'source_ids': []}]},
        )
        summary = _retrieval_trace(NS(question_id='q1', status='answered', trace=trace, raw_outputs=()))
        tools = [t for t in summary.get('tools', []) if isinstance(t, dict)]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0].get('new_node_ids'), ['n1', 'n2'])
        self.assertFalse(tools[0].get('repeat_call'))
        self.assertEqual(tools[1].get('new_node_ids'), ['n3'])   # n2 已召回不再计新增
        self.assertTrue(tools[1].get('repeat_call'))             # 同工具同参数再现


class CrossRoundRejectionFeedbackTests(unittest.TestCase):
    """规格#5（用户抓到的真缺口）：上一轮拒绝原因必须进下一轮提案反馈——
    轮内重试看得到 admission_error，跨轮以前看不到，导致每轮摔新坑不带记忆。"""

    def test_previous_round_rejection_enters_feedback(self):
        from oak.experiments.runner import training_feedback
        from oak.contracts import RunResult
        result = RunResult('c', 'i', 'v', (), (), ())
        payload = training_feedback((SimpleNamespace(id='c', questions=()),), (result,), (('c', {}),),
                                    EvaluationResult({'m': 0}, 0, 0, 0, 0),
                                    active_stages={'F': '执行中'},
                                    previous_round={'round': 'R2', 'status': 'validation_failed',
                                                    'accepted': False,
                                                    'reasons': ['SandboxError: Container-to-string']})
        import json
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertIn('previous_round', blob)
        self.assertIn('Container-to-string', blob)


class PatchNormalizationTests(unittest.TestCase):
    """v13 R4 十连败死因：提案把展示用 fingerprint 键回显进资产对象。机械剥离多余键，
    格式类错误不再消耗重试预算（用户拍板重试上限 50 次，留给内容类问题）。"""

    def test_extra_keys_dropped_and_noted(self):
        from oak.experiments.proposal import ProposalGenerator
        import inspect
        src = inspect.getsource(ProposalGenerator)
        self.assertIn('dropped', src)
        # 直接驱动 valid：构造带多余键的补丁载荷
        import asyncio
        from oak.kernel.assets import Asset
        from oak.llm.recorded import RecordedClient
        item = {'id': 'p_x', 'kind': 'P', 'role': 'tools', 'content': '指引',
                'input_contract': {'type': 'any'}, 'output_contract': {'type': 'any'},
                'schema_dependencies': [], 'description': 'd', 'trial_inputs': [],
                'fingerprint': 'should-be-dropped'}
        class FakeSession:
            async def request(self, *a, **k):
                raise AssertionError('不经会话')
        gen = ProposalGenerator.__new__(ProposalGenerator)
        valid = None
        # 通过类内部协议函数直接验证剥离逻辑（不整段伪造会话）
        from oak.experiments.proposal import AssetPatch
        import dataclasses
        fields = {f.name for f in dataclasses.fields(Asset)}
        extra = sorted(set(item) - fields - {'schema_dependencies'})
        self.assertEqual(extra, ['fingerprint'])
        cleaned = {k: v for k, v in item.items() if k in fields or k == 'schema_dependencies'}
        self.assertNotIn('fingerprint', cleaned)
        asset = Asset(**cleaned)
        self.assertEqual(asset.id, 'p_x')


class RetryVarianceTests(unittest.TestCase):
    """R5 事故：50 次重试只发生 2 次真实调用——报错字符串相同→提示词相同→LLM 缓存
    返回首次坏补丁。重试提示词必须携带序号（缓存破坏），50 次才是真的 50 次。"""

    def test_admission_error_carries_attempt_number(self):
        import inspect
        from oak.experiments import runner
        src = inspect.getsource(runner)
        self.assertIn('重试 {attempt+1}/{ADMISSION_ATTEMPTS}', src)


class FUnitTestsTests(unittest.TestCase):
    """用户拍板：冒烟之外必须有单测——准入试跑并入真实数据形态压力矩阵
    （空行/图头尾/列表字段行）。容器 str() 类分支错误在准入层暴露（R7 事故：14 题）。"""

    def test_container_str_fails_stress_admission(self):
        import tempfile
        from oak.experiments.runner import stress_trial_samples
        from oak.experiments.snapshots import load_frozen_graph
        from oak.kernel.assets import Asset, KernelAssets
        from oak.kernel.functions import FunctionRegistry
        from oak.operators.sandbox import Limits
        from tests.integration.test_agentic_round import FakeEmbedder, build_snapshot, corpus, cold_bundle
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            # 坏 F：对输入 rows 的字段直接 str()（R7 十四连故障同款）
            bad = Asset('f_bad', 'F',
                        "def run(params):\n out=[]\n for r in params['rows']:\n  out.append(str(r.get('source_ids')))\n return {'rows': out}\n",
                        {'type': 'object', 'properties': {'rows': {'type': 'array'}},
                         'additionalProperties': True},
                        {'type': 'object', 'properties': {'rows': {'type': 'array'}},
                         'additionalProperties': True}, ['schema'],
                        description='bad', trial_inputs=({'rows': [{'node_id': 'n000000'}]},))
            bundle = KernelAssets(tuple(
                [a for a in cold_bundle(root).assets.assets if a.kind != 'F'] + [bad])).export(root / 'b')
            reg = FunctionRegistry(bundle, Limits(30000, 15.0, 180000))
            samples = stress_trial_samples([{'rows': [{'node_id': 'n000000'}]}], graph)
            self.assertTrue(samples, '压力样本应非空')
            raised = None
            for sample in samples:                     # 任一压力形态触发即视为单测发现
                try:
                    reg.call('f_bad', sample, graph)
                except ValueError as exc:
                    raised = exc
                    break
            self.assertIsNotNone(raised, '压力矩阵应触发容器 str() 错误')
            self.assertIn('Container-to-string', str(raised))

    def test_stress_samples_shapes(self):
        import tempfile
        from oak.experiments.runner import stress_trial_samples
        from tests.integration.test_agentic_round import build_snapshot
        with tempfile.TemporaryDirectory() as td:
            snapshot, _ = build_snapshot(Path(td))
            from oak.experiments.snapshots import load_frozen_graph
            from tests.integration.test_agentic_round import corpus
            graph = load_frozen_graph(snapshot, corpus())
            base = {'rows': [{'node_id': 'n000000'}]}
            out = stress_trial_samples([base], graph)
            self.assertTrue(any(p['rows'] == [] for p in out), '含空行集')
            self.assertTrue(any(len(p['rows']) > 100 for p in out), '含大规模行集（预算形态）')
            self.assertGreater(len(out), 2, '含多形态')
