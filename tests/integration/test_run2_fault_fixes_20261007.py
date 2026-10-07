"""2026-10-07 运行二事故修复回归。

A 参数契约违规进反馈环（answer.py）：DeepSeek 工具调用发明未声明字段 {'主题'}，
此前 ValueError 一击致命成题级确定性故障（B0 门即挂）。现在 tool.params: 前缀的
契约错误进反馈（含合法字段清单）→ 模型同题重试修正 → 正常发布。
B 外部故障不拦采纳门（policy.py＋runner 验证门，操作者指令「外部异常导致就
不应该拦截」）：传输/限流族生成故障留分母＋披露、不拦；确定性族照拦。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import EvaluationResult
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kg.graph import load_graph
from darwinagent.agents.answer import AnswerAgent

from tests.integration.test_wiki_faults_repro import (FIXTURES, TASK_YAML,
                                                      FIXED_C, FIXED_F,
                                                      base_bundle, candidate_bundle,
                                                      legal_candidate, travel_case)


class ToolParamRejectEntersFeedback(unittest.TestCase):
    """A：未声明参数 → tool_call_reject 反馈（含 allowed_fields）→ 修正后正常作答。"""

    def test_undeclared_param_feedbacks_then_publishes(self):
        case = travel_case()
        question = case.questions[0]
        graph = type('G', (), {'graph': load_graph(FIXTURES / 'graph.json'),
                               'sources': {b.source.id: b for b in case.corpus}})()
        seen_feedback = []

        class StubClient:
            def __init__(self):
                self.tool_turn = 0

            async def chat(self, role, messages, **kwargs):
                if role == RunConfig().tools_role:
                    self.tool_turn += 1
                    if self.tool_turn == 1:
                        content = json.dumps({'action': 'call', 'asset_id': 'f_city_rows',
                                              'parameters': {'table': 'restaurants',
                                                             'city': 'Rockford', 'limit': 5,
                                                             '主题': '餐饮'}})
                    else:
                        # 第二次必须已收到带 allowed_fields 的拒绝反馈并改对参数
                        payload = json.loads(messages[-1]['content'])
                        rejects = [f for f in payload.get('feedback') or []
                                   if 'tool_call_reject' in f]
                        if self.tool_turn == 2:
                            assert rejects, '重试请求必须携带 tool_call_reject 反馈'
                            assert 'allowed_fields' in rejects[0]['tool_call_reject']
                            assert '主题' not in rejects[0]['tool_call_reject']['allowed_fields']
                        seen_feedback.extend(rejects)
                        content = json.dumps({'action': 'call', 'asset_id': 'f_city_rows',
                                              'parameters': {'table': 'restaurants',
                                                             'city': 'Rockford', 'limit': 50}})
                    return type('Reply', (), {'content': content})()
                if role == RunConfig().review_role:
                    return type('Reply', (), {'content': json.dumps(
                        {'accepted': True, 'supported': True, 'subject_correct': True,
                         'consistent': True, 'complete': True, 'abstention_valid': True,
                         'feedback': 'ok'})})()
                payload = json.loads(messages[-1]['content'])
                rows = payload.get('tool_results') or []
                ids = sorted({r for row in rows for r in (row.get('node_ids') or ())})
                return type('Reply', (), {'content': json.dumps(
                    {'status': 'answered', 'answer': legal_candidate()['answer'],
                     'node_ids': ids})})()

            async def aclose(self):
                pass

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        patched = candidate_bundle(base_bundle(),
                                   {'c_answer_shape': FIXED_C,
                                    'f_flight_pair': FIXED_F}, holder.name)
        spec = TaskSpec.load(TASK_YAML, patched)
        config = RunConfig(protocol_attempts=2, answer_attempts=2)
        runtime = KernelRuntime(patched, config)
        agent = AnswerAgent(runtime, StubClient(), config, spec, 'paramretry')
        result = asyncio.run(agent.answer(question, graph))
        self.assertEqual(result.status, 'answered', str(result.error))
        self.assertTrue(seen_feedback, '必须发生过一次契约拒绝反馈')
        self.assertTrue(result.evidence)


class ExternalFaultsDoNotBlockAdoption(unittest.TestCase):
    """B：split_faults 分类＋采纳门只拦确定性族。"""

    @staticmethod
    def _scores(precise, faults=(), total=10):
        diag = tuple({'question_id': str(i), 'status': 'execution_error', 'error': e}
                     for i, e in enumerate(faults))
        completed = total - len(faults)
        return EvaluationResult({'precise': precise, 'lenient': precise}, total, completed,
                                len(faults), 0, diag)

    def test_split_faults_classifies_by_error_prefix(self):
        from darwinagent.experiments.policy import split_faults
        s = self._scores(5, faults=('TransportExhausted: tools: 429', 'RateLimitError: x',
                                    'ValueError: tool.params: undeclared'))
        self.assertEqual(split_faults(s), (2, 1))

    def test_external_fault_does_not_block_deterministic_does(self):
        from darwinagent.experiments.policy import AdoptionPolicy
        policy = AdoptionPolicy('precise', ('lenient',))
        baseline = self._scores(5)
        # 外部故障候选：primary 严格升 → 采纳，且披露 external_faults
        ext = self._scores(6, faults=('TransportExhausted: tools: 429',))
        d = policy.decide(baseline, ext)
        self.assertTrue(d['accepted'], d['reasons'])
        self.assertEqual(d['external_faults'], {'baseline': 0, 'candidate': 1})
        # 确定性故障候选：同分数 → 拒（incomplete_evaluation）
        det = self._scores(6, faults=('ValueError: tool.params: undeclared',))
        d2 = policy.decide(baseline, det)
        self.assertFalse(d2['accepted'])
        self.assertIn('incomplete_evaluation', d2['reasons'])
        # 外部故障不抬分：primary 不升仍拒
        ext_flat = self._scores(5, faults=('TransportExhausted: tools: 429',))
        d3 = policy.decide(baseline, ext_flat)
        self.assertFalse(d3['accepted'])


if __name__ == '__main__':
    unittest.main()
