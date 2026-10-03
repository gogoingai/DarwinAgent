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
from oak.contracts import EvaluationResult
from oak.experiments import AdoptionPolicy, CampaignController, ExperimentSpec, SelectionPolicy
from oak.kernel import KernelBundle, TaskSpec
from oak.kernel.registration import load_assets
from oak.llm.recorded import RecordedClient
from tests.fixtures import TASK, review

SERIALS = {'train-case': 'D-17', 'val-case': 'D-18', 'test-case': 'D-19'}


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
    return {'extraction': [{'facts': [{'text': f'设备 {serial} 于 2026-09-01 由林维护',
        'subject': {'class': 'device', 'name': serial}, 'predicate': '维护',
        'object': {'entity': {'class': 'person', 'name': '林'}},
        'polarity': 'positive', 'modality': 'statement',
        'time': {'raw': '2026-09-01', 'precision': 'day', 'start': '2026-09-01', 'end': '', 'relative': False},
        'evidence': [{'source_id': 'm0', 'quote': block.text}]}]}],
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
        return EvaluationResult({'precise': good, 'lenient': good}, n, n, 0, 0)


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
                'training_evidence': ['q1']}]}]}
        else:
            declaration = json.loads((self.root / 'campaign.json').read_text())['declaration']['experiment_spec']
            phase = 'train' if name.startswith('R') or name == 'B0' else \
                ('validation' if 'validation' in str(stage_dir) else 'test')
            replies = generation_replies(declaration[phase][0])
        return LedgerRecordedClient(replies)


def protocol(rounds, cap=None):
    return ExperimentSpec(train=('train-case',), validation=('val-case',), test=('test-case',),
                          rounds=rounds, adoption=AdoptionPolicy('precise', ('lenient',)),
                          selection=SelectionPolicy('precise', 'lenient'),
                          max_question_runs=cap)


def execute(spec, resume=False, stop=False, root=None):
    td = None
    if root is None:
        td = tempfile.TemporaryDirectory(); root = Path(td.name)
    (root / 'precheck.json').write_text(json.dumps({'passed': True, 'checks': {}}))
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

    def test_resume_is_idempotent(self):
        root, td, summary = execute(protocol(rounds=1))
        self.addCleanup(td.cleanup)
        again_root, again_td, again = execute(protocol(rounds=1), resume=True, root=root)
        self.assertEqual(again['selected'], summary['selected'])
        self.assertEqual(again['question_runs'], summary['question_runs'])
        self.assertEqual(again['status'], 'complete')

    def test_missing_precheck_refuses_to_start(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        controller = RecordedCampaign(root, spec=protocol(rounds=1), frozen_files=())
        with self.assertRaises(ValueError):
            asyncio.run(controller.run(TaskSpec.load(TASK / 'task.yaml')))


if __name__ == '__main__':
    unittest.main()
