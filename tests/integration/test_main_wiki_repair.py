"""Regression probes for the four bugs found after merging Wiki (no real model)."""
import asyncio
from dataclasses import replace
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.graph_rules import _session_dates
from datasets.locomo.scripts import question_split
from oak.experiments.proposal import ProposalGenerator
from oak.experiments.wiki import WikiMaintainer, _lessons
from oak.kernel import TaskSpec
from oak.runtime.deadline import ROUND_DEADLINE, RoundDeadlineExceeded
from oak.vector.embedder import Embedder
from tests.fixtures import TASK
from tests.integration.test_fastloop_mode import FastLoopExperiment, _run


class FullRoundDeadlineTests(unittest.TestCase):
    def verify_timeout(self, root, summary):
        decision = summary['rounds'][0]
        self.assertEqual(decision['status'], 'round_timeout')
        self.assertFalse(decision['accepted'])
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['completed_rounds'], 0)
        pointer = json.loads((root / 'published/current.json').read_text())
        self.assertEqual(pointer['version'], decision['base_version'])
        self.assertTrue((root / 'R1/timeout.json').exists())

    def test_inflight_proposal_cancelled_before_formal_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = ProposalGenerator.propose
            cancelled = []
            async def slow(self, *args, **kwargs):
                try:
                    await asyncio.sleep(0.6)
                    return await original(self, *args, **kwargs)
                finally:
                    cancelled.append(True)
            with mock.patch.object(ProposalGenerator, 'propose', slow):
                summary = _run(FastLoopExperiment(root, round_deadline_s=0.3), rounds=1)
            self.verify_timeout(root, summary)
            self.assertTrue(cancelled)
            self.assertFalse((root / 'R1/stage.json').exists())

    def test_formal_score_overrun_does_not_publish(self):
        class SlowFormal(FastLoopExperiment):
            async def _stage(self, name, *args, **kwargs):
                if name == 'R1':
                    await asyncio.sleep(0.6)
                return await super()._stage(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = _run(SlowFormal(root, round_deadline_s=0.3), rounds=1)
            self.verify_timeout(root, summary)
            self.assertFalse((root / 'R1/stage.json').exists())

    def test_wiki_overrun_does_not_publish_completed_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = WikiMaintainer.record
            async def slow(self, stage, kind, *args, **kwargs):
                if stage == 'R1' and kind == 'decision' and kwargs.get('infer'):
                    await asyncio.sleep(0.6)
                return await original(self, stage, kind, *args, **kwargs)
            with mock.patch.object(WikiMaintainer, 'record', slow):
                summary = _run(FastLoopExperiment(root, round_deadline_s=0.3), rounds=1)
            self.verify_timeout(root, summary)
            self.assertTrue((root / 'R1/stage.json').exists())

    def test_resume_keeps_consumed_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=0.001)
            _run(runner, rounds=1)
            budget_path = root / 'R1/round-budget.json'
            budget = json.loads(budget_path.read_text())
            # Simulate interruption after budget exhaustion but before terminal decision.
            (root / 'R1/decision.json').unlink()
            resumed = FastLoopExperiment(root, round_deadline_s=0.001)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(resumed.run(resumed.case.id, TaskSpec.load(TASK / 'task.yaml'),
                                                  rounds=1, resume=True, scope=('S', 'F', 'C', 'P')))
            self.verify_timeout(root, summary)
            self.assertEqual(json.loads(budget_path.read_text()), budget)

    def test_native_embedding_uses_remaining_budget_without_retries(self):
        calls = []
        def post(*args, **kwargs):
            calls.append(kwargs['timeout'])
            time.sleep(0.025)
            return mock.Mock(status_code=200, json=lambda: {'data': [{'embedding': [1.0]}]})
        token = ROUND_DEADLINE.set(time.monotonic() + 0.015)
        try:
            with mock.patch('oak.vector.embedder.httpx.post', post), \
                    mock.patch('oak.vector.embedder.time.sleep', wraps=time.sleep):
                with self.assertRaises(RoundDeadlineExceeded):
                    Embedder('https://unused.example', 'unused', 'recorded').embed('probe')
            self.assertEqual(len(calls), 1)
            self.assertLessEqual(calls[0], 0.015)
        finally:
            ROUND_DEADLINE.reset(token)


class WholeEvidenceGroupTests(unittest.TestCase):
    def test_actual_default_split_and_seeds_have_no_shared_evidence(self):
        qs = next(q for q in json.loads(question_split.DATA.read_text())
                  if q['sample_id'] == 'conv-26')['qa']
        for seed in (20261005, 0, 1, 42):
            with self.subTest(seed=seed):
                split = question_split.build_split(seed=seed)
                self.assertEqual((len(split['train']), len(split['validation'])), (15, 10))
                train = {e for i in split['train'] for e in qs[i].get('evidence') or ()}
                val = {e for i in split['validation'] for e in qs[i].get('evidence') or ()}
                self.assertFalse(train & val)
                self.assertEqual(split, question_split.build_split(seed=seed))

    def test_transitive_evidence_groups_across_categories_stay_whole(self):
        qs = [{'category': 1, 'evidence': ['A']},
              {'category': 2, 'evidence': ['A', 'B']},
              {'category': 3, 'evidence': ['B']},
              {'category': 1, 'evidence': ['C']},
              {'category': 3, 'evidence': ['C']}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.json'
            path.write_text(json.dumps([{'sample_id': 'recorded', 'qa': qs}]))
            with mock.patch.object(question_split, 'DATA', path):
                split = question_split.build_split('recorded', train_n=3, val_n=2)
                self.assertEqual(split['train'], [0, 1, 2])
                self.assertEqual(split['validation'], [3, 4])
                with self.assertRaisesRegex(ValueError, 'without splitting'):
                    question_split.build_split('recorded', train_n=2, val_n=2)


class ConversationDateTests(unittest.TestCase):
    def test_all_session_dates_follow_message_metadata(self):
        case = LocomoAdapter(Path('datasets/locomo/data/locomo10_zh.json')).generation_input('conv-26')
        from datasets.locomo.graph_rules import load_facts
        facts, _ = load_facts(Path('datasets/locomo/snapshots/gvtest_v1/conv-26'))
        dates = _session_dates(facts, case.corpus)
        self.assertEqual(dates[1], '2023-05-08')
        for block in case.corpus:
            n = int(block.source.location.split(':')[0][1:])
            self.assertEqual(dates[n], block.metadata['date'][:10])
        altered = [{**f, 'date_iso': '1999-01-01'} for f in facts]
        self.assertEqual(_session_dates(altered, case.corpus), dates)

    def test_missing_or_conflicting_record_dates_are_not_event_dates(self):
        facts = [{'fid': 'x', 'session_no': 1, 'date_iso': '2023-05-07'}]
        self.assertEqual(_session_dates(facts), {})
        case = LocomoAdapter(Path('datasets/locomo/data/locomo10_zh.json')).generation_input('conv-26')
        block = case.corpus[0]
        conflict = replace(block, metadata={**block.metadata, 'date': '1999-01-01'})
        with self.assertRaisesRegex(ValueError, '日期冲突'):
            _session_dates(facts, (block, conflict))


class ColonCaseBindingTests(unittest.TestCase):
    def lessons(self, target_case='train:0', target_scenario='stress', target_graph='g1'):
        from tests.integration.test_wiki_check_replay import FourthReviewBindingTests
        helper = FourthReviewBindingTests
        h = helper.DIGEST
        return _lessons([
            helper._failed(f'train:0:stress:0:{h}', graph_digests={'train:0': 'g1'}),
            helper._passed([{'asset_id': 'f_flight_pair', 'scenario_id': target_scenario,
                             'status': 'passed', 'required': True,
                             'input_ref': f'{target_case}:{target_scenario}:0:{h}'}],
                           graph_digests={target_case: target_graph})])

    def test_colon_case_different_scenario_graph_or_case_cannot_verify(self):
        for kwargs in ({'target_scenario': 'base'}, {'target_graph': 'g2'},
                       {'target_case': 'train:1'}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(any(l['status'] == 'admission_verified' for l in self.lessons(**kwargs)))

    def test_matching_colon_case_verifies_actual_reproduction(self):
        self.assertTrue(any(l['status'] == 'admission_verified' for l in self.lessons()))


if __name__ == '__main__':
    unittest.main()
