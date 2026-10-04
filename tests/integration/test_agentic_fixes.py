"""评审修复回归（2026-10-04）：分批重试统计、检索工具底线、提案错误回灌、外测聚合。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oak.config import RunConfig
from oak.contracts import AnswerResult, CaseInput, EvaluationResult, QuestionInput, RunResult, SourceRef
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
            async def evaluate(self, result):
                evaluated.append([a.status for a in result.answers])
                return EvaluationResult({'m': 1}, 2, 2, 0, 0)

        async def fake_sleep(_s): pass
        original_retry = R.batched_fault_retry

        async def fast_retry(*args, **kwargs):
            kwargs['sleep'] = fake_sleep
            return await original_retry(*args, **kwargs)

        class C: id = 'c'

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
        class C: id='c'
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
        class C: id='c'
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
