"""Full three-set campaign state machine on recorded transport."""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from oak.config import Config, RunConfig
from oak.experiments.spec import precheck_identity
from oak.contracts import EvaluationResult
from oak.experiments import AdoptionPolicy, CampaignController, ExperimentSpec, SelectionPolicy
from oak.kernel import KernelBundle, TaskSpec
from oak.kernel.registration import load_assets
from oak.llm.recorded import RecordedClient
from tests.fixtures import TASK, review

SERIALS = {'train-case': 'D-17', 'train-case-2': 'D-27', 'val-case': 'D-18', 'val-case-2': 'D-28', 'test-case': 'D-19', 'test-case-2': 'D-29'}


class StubAdapter:
    def generation_input(self, case_id):
        from oak.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
        serial = SERIALS[case_id]
        return CaseInput(case_id, (CorpusBlock(SourceRef('maintenance_record', case_id, 'row-1'),
            f'设备 {serial} 于 2026-09-01 由林维护。'),),
            (QuestionInput('q1', f'谁在什么时候维护了 {serial}？', {'serial': serial}),))


class LedgerRecordedClient(RecordedClient):
    async def aclose(self): pass
    def ledger_summary(self): return {'total_calls': len(self.calls)}


def generation_replies(case_id):
    serial = SERIALS[case_id]
    block = StubAdapter().generation_input(case_id).corpus[0]
    return {'extraction': [{'entities': [{'type': 'Maintenance', 'key': {'serial': serial, 'date': '2026-09-01'},
                                          'properties': {'technician': '林'}, 'source_id': block.source.id,
                                          'quote': block.text}], 'relations': []}],
            'tools': [{'action': 'call', 'asset_id': 'device_lookup', 'parameters': {'serial': serial}},
                      {'action': 'ready'}],
            'answer': [{'status': 'answered', 'answer': '林于2026-09-01维护。', 'node_ids': ['n000000']}],
            'review': [review()]}


class VersionAwareEvaluator:
    """Anything produced after B0 (an adopted candidate) scores strictly better."""

    def __init__(self, client, path, root):
        self.client, self.path, self.root = client, Path(path), Path(root)

    async def evaluate(self, result):
        b0 = json.loads((self.root / 'train' / 'B0' / 'assets' / 'manifest.json').read_text())['version']
        good = 1 if result.asset_version != b0 else 0
        n = len(result.answers)
        faults = sum(a.status == 'execution_error' for a in result.answers)
        return EvaluationResult({'precise': good, 'lenient': good}, n, n - faults, faults, 0)


class RecordedCampaign(CampaignController):
    def __init__(self, root, spec, **kwargs):
        self.stage_clients = Counter()
        super().__init__(StubAdapter(), lambda client, path: VersionAwareEvaluator(client, path, root),
                         Config(), RunConfig(protocol_attempts=1), spec=spec, work_dir=root,
                         client_factory=self._recorded_client, **kwargs)

    def _recorded_client(self, stage_dir):
        name = stage_dir.name if isinstance(stage_dir, Path) else str(stage_dir)
        self.stage_clients[name] += 1
        if self.stage_clients[name] == 1 and name == 'B0':
            replies = {'bootstrap': [{'assets': [a.to_dict() for a in load_assets(TASK).assets]}]}
        elif self.stage_clients[name] == 1 and name.startswith('R'):
            pointer = json.loads((self.root / 'train' / 'published' / 'current.json').read_text())
            base = KernelBundle(self.root / 'train' / 'published' / pointer['path'])
            asset = next(a for a in base.assets.assets if a.role == 'answer')
            updated = asset.to_dict(); updated['content'] += '\nUse the records carefully. ' + name
            replies = {'proposal': [{'patches': [{'asset': updated, 'base_fingerprint': asset.fingerprint,
                'reason': 'General instruction refined from this training run',
                'training_evidence': ['train-case::q1']}]}]}
        else:
            declaration = json.loads((self.root / 'campaign.json').read_text())['declaration']['experiment_spec']
            phase = 'train' if name.startswith('R') or name == 'B0' else \
                ('validation' if 'validation' in str(stage_dir) else 'test')
            if name in SERIALS:
                # 逐 case 客户端（验证/测试）：只装载本 case 的回复
                replies = generation_replies(name)
            else:
                # 训练阶段客户端：按声明顺序装载全部 case 的回复
                replies = {}
                for case_id in declaration[phase]:
                    for role, values in generation_replies(case_id).items():
                        replies.setdefault(role, []).extend(values)
        return LedgerRecordedClient(replies)


def protocol(rounds, cap=None, multi=False):
    if multi:
        return ExperimentSpec(train=('train-case', 'train-case-2'),
                              validation=('val-case', 'val-case-2'),
                              test=('test-case', 'test-case-2'),
                              rounds=rounds, adoption=AdoptionPolicy('precise', ('lenient',)),
                              selection=SelectionPolicy('precise', 'lenient'),
                              max_question_runs=cap)
    return ExperimentSpec(train=('train-case',), validation=('val-case',), test=('test-case',),
                          rounds=rounds, adoption=AdoptionPolicy('precise', ('lenient',)),
                          selection=SelectionPolicy('precise', 'lenient'),
                          max_question_runs=cap)


def execute(spec, resume=False, stop=False, root=None):
    td = None
    if root is None:
        td = tempfile.TemporaryDirectory(); root = Path(td.name)
    (root / 'precheck.json').write_text(json.dumps({'passed': True, 'checks': {},
                                                        'identity': precheck_identity(Config(), RunConfig(protocol_attempts=1))}))
    if stop: (root / 'STOP').write_text('operator stop\n')
    controller = RecordedCampaign(root, spec=spec, frozen_files=())
    task = TaskSpec.load(TASK / 'task.yaml')
    with contextlib.redirect_stdout(io.StringIO()):
        summary = asyncio.run(controller.run(task, resume=resume))
    return root, td, summary


class ThreeSetCampaign(unittest.TestCase):
    def test_adopt_validate_select_test_and_ledger(self):
        root, td, summary = execute(protocol(rounds=1))
        self.addCleanup(td.cleanup)
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual([d['accepted'] for d in summary['train']['rounds']], [True])
        b0 = summary['candidates'][0]['version']
        self.assertEqual(len(summary['candidates']), 2)
        self.assertNotEqual(summary['selected'], b0)
        self.assertEqual(len(summary['test']), 2)
        self.assertEqual(summary['question_runs'], 6)  # 2 train + 2 validation + 2 test
        selected = json.loads((root / 'selected.json').read_text())
        self.assertTrue(selected['sealed_before_test'])
        self.assertEqual(selected['selected'], summary['selected'])

    def test_operator_stop_locks_b0_and_single_test_run(self):
        root, td, summary = execute(protocol(rounds=None), stop=True)
        self.addCleanup(td.cleanup)
        self.assertEqual(summary['status'], 'complete')
        self.assertTrue(summary['operator_stopped'])
        self.assertEqual(len(summary['candidates']), 1)      # B0 only
        self.assertIsNone(summary['selection']['selected'])  # nothing beats B0
        self.assertEqual(summary['selected'], summary['candidates'][0]['version'])
        self.assertEqual(len(summary['test']), 1)            # identical fingerprints run once
        self.assertEqual(summary['question_runs'], 3)        # 1 train + 1 validation + 1 test

    def test_safety_cap_blocks_before_overshoot(self):
        with self.assertRaises(ValueError):
            execute(protocol(rounds=1, cap=5))

    def test_resume_after_completion_is_sealed(self):
        root, td, summary = execute(protocol(rounds=1))
        self.addCleanup(td.cleanup)
        again = json.loads((root / 'campaign-summary.json').read_text())
        with self.assertRaises(ValueError) as caught:
            execute(protocol(rounds=1), resume=True, root=root)
        self.assertIn('sealed', str(caught.exception))
        # The sealed summary on disk is untouched by the refused resume.
        self.assertEqual(json.loads((root / 'campaign-summary.json').read_text()), again)

    def test_multi_case_splits_aggregate_and_ledger(self):
        root, td, summary = execute(protocol(rounds=1, multi=True))
        self.addCleanup(td.cleanup)
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(len(summary['candidates']), 2)
        self.assertNotEqual(summary['selected'], summary['candidates'][0]['version'])
        # 每阶段 2 case × 1 题：训练 2 阶段 + 验证 2 候选 + 测试 2 版本 = 12 题次
        self.assertEqual(summary['question_runs'], 12)
        # 验证聚合：B0 两 case 各 0 分、候选各 1 分 → 聚合 0 vs 2
        b0 = summary['candidates'][0]['version']
        self.assertEqual(summary['validation'][b0]['metrics'], {'precise': 0, 'lenient': 0})
        cand = summary['selected']
        self.assertEqual(summary['validation'][cand]['metrics'], {'precise': 2, 'lenient': 2})
        self.assertEqual(summary['validation'][b0]['total'], 2)

    def test_precheck_wrong_identity_refused(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / 'precheck.json').write_text(json.dumps(
            {'passed': True, 'checks': {},
             'identity': {'transport': {}, 'config': 'deadbeef', 'framework': 'deadbeef'}}))
        controller = RecordedCampaign(root, spec=protocol(rounds=1))
        from oak.kernel import TaskSpec
        with self.assertRaises(ValueError) as caught:
            asyncio.run(controller.run(TaskSpec.load(TASK / 'task.yaml')))
        self.assertIn('identity mismatch', str(caught.exception))

    def test_missing_precheck_refuses_to_start(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1), frozen_files=())
        with self.assertRaises(ValueError):
            asyncio.run(controller.run(TaskSpec.load(TASK / 'task.yaml')))


if __name__ == '__main__':
    unittest.main()
