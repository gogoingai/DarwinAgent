"""验证缺口修复回归（2026-10-06，①③⑤⑥＋传输重试次数）。

① rounds 移出运行身份＋计分轮口径：超时/耗尽迭代不占轮数、旧声明含 rounds 键可续跑；
③ seed-assets：锁定 bundle 直接锚定 B0（不冷启动、门槛不豁免、进身份）；
⑤⑥ 发布点契约错误进反馈环：引用无出处行→反馈（含逐节点来源数）→重答成功。
传输重试默认 10 次另在 test_transient_faults 覆盖（此处只断言默认值）。
"""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from networkx import freeze
from oak.config import RunConfig
from oak.contracts import GraphResult
from oak.kernel import KernelBundle, TaskSpec
from oak.kernel.execution import KernelRuntime
from oak.kernel.registration import load_assets
from tests.fixtures import TASK
from tests.integration.test_experiment import RecordedExperiment
from tests.integration.test_wiki_faults_repro import (FIXTURES, TASK_YAML,
                                                      FIXED_C, FIXED_F,
                                                      base_bundle,
                                                      candidate_bundle,
                                                      legal_candidate,
                                                      travel_case)
from oak.kg.graph import load_graph
from oak.agents.answer import AnswerAgent


class MaxRetriesDefault(unittest.TestCase):
    def test_transport_retries_now_ten(self):
        from oak.config import Config
        cfg = Config(api_key='x', work_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(cfg.max_retries, 10)


class PublishRejectEntersFeedback(unittest.TestCase):
    """⑤⑥：引用无出处事实行 → 发布点契约错误 → 反馈（逐节点来源数）→ 换行重答成功。"""

    def test_unsourced_citation_retries_then_publishes(self):
        case = travel_case()
        question = case.questions[0]
        g = load_graph(FIXTURES / 'graph.json')
        # 从工具真实可见的 Rockford 餐厅行里选节点（候选校验要求 node_ids ⊆ visible）
        from oak.operators.data import DataCapabilities
        probe = GraphResult(freeze(g), {b.source.id: b for b in case.corpus})
        caps = DataCapabilities(probe)
        visible_rests = [rid for rid, r in caps.rows.items()
                         if r.get('entity_type') == 'restaurant'
                         and 'Rockford' in str(r.get('city', ''))][:2]
        unsourced, healthy = visible_rests
        # 行 id(n…)->nx 节点键映射，无出处清的是底层节点的 __sources__
        g.nodes[caps.actual_ids[unsourced]]['__sources__'] = []
        graph = GraphResult(freeze(g), {b.source.id: b for b in case.corpus})

        seen_feedback = []

        class StubClient:
            def __init__(self):
                self.tool_turn = 0
                self.answer_turn = 0

            async def chat(self, role, messages, **kwargs):
                if role == RunConfig().tools_role:
                    self.tool_turn += 1
                    if self.tool_turn == 1:
                        content = json.dumps({'action': 'call', 'asset_id': 'f_city_rows',
                                              'parameters': {'table': 'restaurants',
                                                             'city': 'Rockford', 'limit': 50}})
                    else:
                        content = json.dumps({'action': 'ready'})
                    return type('Reply', (), {'content': content})()
                payload = json.loads(messages[-1]['content'])
                if role == RunConfig().review_role:
                    return type('Reply', (), {'content': json.dumps(
                        {'accepted': True, 'supported': True, 'subject_correct': True,
                         'consistent': True, 'complete': True, 'abstention_valid': True,
                         'feedback': 'ok'})})()
                self.answer_turn += 1
                if self.answer_turn == 1:
                    ids = [unsourced]
                else:
                    ids = [healthy]
                    seen_feedback.append(payload.get('feedback'))
                return type('Reply', (), {'content': json.dumps(
                    {'status': 'answered', 'answer': legal_candidate()['answer'],
                     'node_ids': ids})})()

            async def aclose(self):
                pass

        spec = TaskSpec.load(TASK_YAML, base_bundle())
        config = RunConfig(protocol_attempts=2, answer_attempts=3)
        # base_bundle 的旧版 C 会误杀归档合法候选（T2 归档故障）——换修复版 C/F 的
        # 补丁 bundle，本测试聚焦发布点反馈环本身。
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        patched = candidate_bundle(base_bundle(),
                                   {'c_answer_shape': FIXED_C,
                                    'f_flight_pair': FIXED_F}, holder.name)
        spec = TaskSpec.load(TASK_YAML, patched)
        runtime = KernelRuntime(patched, config)
        agent = AnswerAgent(runtime, StubClient(), config, spec, 'pubretry')
        result = asyncio.run(agent.answer(question, graph))
        self.assertEqual(result.status, 'answered', str(result.error))
        self.assertTrue(result.evidence, '第二次候选必须带证据发布')
        # 第一次的发布拒绝以反馈形式进入第二次请求，且逐节点来源数明示 0
        self.assertTrue(seen_feedback and seen_feedback[0], '重答请求必须携带发布拒绝反馈')
        pub = [f for f in seen_feedback[0] if 'publish_reject' in f]
        self.assertTrue(pub, seen_feedback[0])
        counts = pub[0]['publish_reject']['cited_node_source_counts']
        self.assertEqual(counts.get(unsourced), 0)
        self.assertEqual(agent.namespace, 'pubretry')


class ScoredRoundsAndIdentity(unittest.TestCase):
    """①：计分轮口径＋rounds 移出身份（含旧声明 rounds 键兼容）。"""

    @staticmethod
    def _lenient_evaluator(transport, path):
        stage = Path(path)
        name = stage.parent.name
        if not (name == 'B0' or name.startswith('R')):
            name = stage.parent.parent.name

        class _Eval:
            async def evaluate(self, result, asked=None):
                from oak.contracts import EvaluationResult
                precise = 0 if name == 'B0' else 1
                return EvaluationResult({'precise': precise}, 1, 1, 0, 0)
        return _Eval()

    def test_timeout_iteration_does_not_consume_round(self):
        import asyncio as _aio
        from tests.integration.test_fastloop_mode import FastLoopExperiment, _run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=2.0,
                                        evaluator=self._lenient_evaluator,
                                        validation_plan={'case': None, 'policy': None})
            runner.validation_plan['case'] = runner.case
            from oak.experiments.spec import SelectionPolicy
            runner.validation_plan['policy'] = SelectionPolicy('precise', 'precise')
            from oak.experiments.proposal import ProposalGenerator
            original = ProposalGenerator.propose

            async def stall_r1_then_fail(self, *args, **kwargs):
                if 'R1' in str(args[5]):
                    await _aio.sleep(2.1)  # 越过 2.0s 轮预算 → attempt-0 失败后再查即超时
                    raise ValueError('static candidate shape invalid')
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, 'propose', stall_r1_then_fail):
                summary = _run(runner, rounds=1)
            # R1 超时不占轮数；循环继续到 R2 计分完成（scored=1 即止）
            statuses = [(r.get('status'), bool((r.get('candidate') or {}).get('metrics')))
                        for r in summary['rounds']]
            self.assertEqual(statuses[0][0], 'round_timeout')
            scored = sum(1 for _, s in statuses if s)
            self.assertGreaterEqual(scored, 1, statuses)

    def test_legacy_declaration_with_rounds_key_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner_cls = type('SeededOnce', (RecordedExperiment,),
                              {'optimization_mode': 'wiki'})
            r1 = runner_cls(root)
            spec = TaskSpec.load(TASK / 'task.yaml')
            with contextlib.redirect_stdout(io.StringIO()):
                s1 = asyncio.run(r1.run(r1.case.id, spec, rounds=1,
                                        scope=('S', 'F', 'C', 'P')))
            self.assertEqual(s1['status'], 'complete')
            # 旧格式声明（含 rounds 键）在移除 rounds 出身份后仍可续跑
            ep = root / 'experiment.json'
            decl = json.loads(ep.read_text())
            decl['rounds'] = 1
            ep.write_text(json.dumps(decl, ensure_ascii=False, default=str))
            r2 = runner_cls(root)
            with contextlib.redirect_stdout(io.StringIO()):
                s2 = asyncio.run(r2.run(r2.case.id, spec, rounds=2, resume=True,
                                        scope=('S', 'F', 'C', 'P')))
            self.assertEqual(len(s2['rounds']), 2, s2['rounds'])
            self.assertNotIn('rounds', json.loads(ep.read_text()))


class SeedAssetsAnchor(unittest.TestCase):
    """③：锁定 bundle 锚定 B0——不冷启动、seed 入身份、门槛照跑。"""

    def test_seed_skips_bootstrap_and_records_identity(self):
        from tests.integration.test_fastloop_mode import FastLoopExperiment, _run
        with tempfile.TemporaryDirectory() as tmp:
            holder = Path(tmp) / 'seed'
            seed_dir = load_assets(TASK).export(holder / 'bundle').root
            root = Path(tempfile.mkdtemp(dir=tmp))

            class SeededRun(FastLoopExperiment):
                def _client(self, stage):
                    if stage == 'B0':
                        # 种子路径没有冷启动：第一份客户端不再留给 bootstrap 回复
                        self.stage_clients[stage] += 1
                    return super()._client(stage)

            runner = SeededRun(root, seed_assets=seed_dir)
            with mock.patch('oak.experiments.bootstrap.AssetBootstrapper',
                            side_effect=AssertionError('冷启动不应执行')):
                summary = _run(runner, rounds=0)
            self.assertEqual(summary['status'], 'complete')
            self.assertTrue((root / 'B0' / 'assets' / 'manifest.json').exists())
            seed_record = json.loads((root / 'B0' / 'seed.json').read_text())
            self.assertEqual(seed_record['version'], KernelBundle(seed_dir).version)
            decl = json.loads((root / 'experiment.json').read_text())
            self.assertEqual(len(decl['seed_assets']), 1)
            self.assertFalse((root / 'B0' / 'bootstrap-call.json').exists())


if __name__ == '__main__':
    unittest.main()
