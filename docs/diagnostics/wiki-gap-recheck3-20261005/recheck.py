"""Independent, offline recheck. Read archived inputs, write only this report directory.

No real LLM requests, no writes to campaign state or live source.
Run from the Wiki worktree with PYTHONPATH=.
"""
import asyncio
import copy
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from oak.config import RunConfig
from oak.experiments.admission import AdmissionError, _samples, admit_candidate
from oak.experiments.wiki import WikiMaintainer, _lessons
from oak.kernel import TaskSpec, KernelBundle
from oak.kernel.checks import CheckRegistry
from oak.kernel.functions import FunctionRegistry
from oak.kernel.execution import KernelRuntime
from oak.operators.sandbox import Limits
from tests.integration.test_experiment import LedgerRecordedClient
from tests.integration.test_wiki_faults_repro import (
    TASK_YAML, COMPLETE_C, IDEAL_C, FIXED_F, base_bundle, candidate_bundle,
    legal_candidate, travel_case, travel_graph,
)

HERE = Path(__file__).resolve().parent
OUTPUT = {}
SOURCE_PATHS = [Path('oak/experiments/wiki.py'), Path('oak/experiments/admission.py'),
                Path('oak/experiments/runner.py'), Path('oak/kernel/checks.py'),
                Path('tasks/travel_planning/task.yaml')]


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def archived_attribution():
    results = []
    root = Path('datasets/travelplanner/runs/wiki_gap_repair_20261005_loop3/train')
    for p in sorted((root / 'optimization/events').glob('*.json')):
        e = json.loads(p.read_text())
        if e['kind'] not in ('formal', 'decision'):
            continue
        client = LedgerRecordedClient({'wiki_maintainer': [{
            'cause': 'Offline probe attribution', 'action': 'Review bounded facts',
            'training_ids': e['training_ids']}]})
        with tempfile.TemporaryDirectory() as tmp:
            m = WikiMaintainer(tmp, 'offline-recheck', lambda _: client,
                               RunConfig(protocol_attempts=1), limit=10)
            from oak.experiments.wiki import _compress_training_evidence
            captured = {}
            def capture(facts, **kwargs):
                _compress_training_evidence(facts, **kwargs)
                captured['facts_chars'] = len(json.dumps(facts, ensure_ascii=False))
                captured['examples'] = [{k: len(json.dumps(v, ensure_ascii=False))
                                         for k, v in example.items()}
                                        for example in facts.get('training_examples', [])]
            with mock.patch('oak.experiments.wiki._compress_training_evidence', capture):
                eid = await m.record(e['stage'], e['kind'], e['facts'],
                                     training_ids=e['training_ids'], infer=True,
                                     category=e['category'], scope=e['scope'])
            target = m.root / 'maintenance' / f'{eid}.json'
            request = m.root / 'maintenance' / f'{eid}.request.json'
            failure = m.root / 'maintenance' / f'{eid}.failure.json'
            results.append({'source': str(p), 'stage': e['stage'], 'kind': e['kind'],
                            'attribution_written': target.exists(),
                            'model_calls': len(client.calls),
                            'request_chars': len(json.dumps(json.loads(request.read_text()),
                                                           ensure_ascii=False)) if request.exists() else None,
                            'after_compression': captured,
                            'failure': json.loads(failure.read_text()) if failure.exists() else None})
    OUTPUT['archived_attribution'] = results


def admission_and_lessons():
    case = travel_case()
    graph = travel_graph(case)
    base = base_bundle()
    spec = TaskSpec.load(TASK_YAML)
    # Answered container check is deliberately broken, abstention still accepted.
    buggy_c = COMPLETE_C.replace('isinstance(ans, (list, tuple))', 'isinstance(ans, list)')
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bundles = {}
        for name, c in [('ideal', IDEAL_C), ('container_bug', buggy_c),
                        ('always_reject_answered', "def check(candidate):\n    if candidate.get('status') == 'abstained':\n        return {'ok': True, 'issues': []}\n    return {'ok': False, 'issues': ['reject all answered']}\n")]:
            b = candidate_bundle(base, {'c_answer_shape': c, 'f_flight_pair': FIXED_F}, root / name)
            bundles[name] = b
            report_path = root / name / 'admission.json'
            try:
                admit_candidate(b, [case], {case.id: graph}, RunConfig(), (), report_path,
                                answer_contract=spec.answer_contract,
                                answer_counterexamples=spec.answer_counterexamples,
                                answer_examples=spec.answer_examples)
            except AdmissionError:
                pass
            report = json.loads(report_path.read_text())
            candidate = legal_candidate()
            _, real = KernelRuntime(b, RunConfig()).check_candidate(
                case.questions[0], candidate, graph, set(candidate['node_ids']))
            result = {'candidate': name, 'admission_verdict': report['verdict'],
                      'real_candidate_opinions': real,
                      'answer_scenarios': [s for s in report['scenarios']
                                          if s['scenario_id'].startswith('answer')]}
            results.append(result)
        OUTPUT['check_admission'] = results

        asset = next(a for a in bundles['container_bug'].assets.assets if a.id == 'c_answer_shape')
        old = {'id': 'old', 'stage': 'R1', 'kind': 'attempt', 'scope': 'admission',
               'facts': {'status': 'failed', 'admission': {'scenarios': [{
                   'asset_id': asset.id, 'scenario_id': 'answer_0', 'status': 'failed',
                   'required': True, 'input_ref': 'case-old:answer_0',
                   'parameters': {'query': 'old'}, 'error_type': 'CandidateCheckRejected',
                   'error': 'answer is not an array'}]}}}
        good = {'id': 'new', 'stage': 'R2', 'kind': 'attempt', 'scope': 'admission',
                'facts': {'status': 'passed', 'asset_changes': [
                    {'after': {**asset.to_dict(), 'fingerprint': asset.fingerprint}}],
                          'verification': {'verdict': 'passed', 'scenarios': [{
                              'asset_id': asset.id, 'scenario_id': 'answer_0',
                              'status': 'passed', 'required': True,
                              'input_ref': 'case-new:answer_0',
                              'parameters': {'query': 'new'}, 'ok': True}]}}}
        different = _lessons([old, good])
        actual_report = next(r for r in results if r['candidate'] == 'container_bug')
        same_but_rejected = copy.deepcopy(good)
        row = copy.deepcopy(next(r for r in actual_report['answer_scenarios']
                                 if r['scenario_id'] == 'answer_0'))
        row['input_ref'] = 'case-old:answer_0'
        same_but_rejected['facts']['verification']['scenarios'] = [row]
        still_rejected = _lessons([old, same_but_rejected])
        OUTPUT['lesson_verification'] = {
            'different_input_ref_marked_verified': any(l['status'] == 'admission_verified'
                                                       for l in different),
            'C_ok_false_marked_verified': any(l['status'] == 'admission_verified'
                                             for l in still_rejected),
            'actual_admission_row': row, 'different_input_lessons': different,
            'still_rejected_lessons': still_rejected}


def numeric_pressure():
    from datasets.locomo.adapter import LocomoAdapter
    from oak.experiments.snapshots import load_frozen_graph
    b = KernelBundle('datasets/locomo/runs/wiki_gap_repair_20261005_mc/train/B0/assets')
    a = next(a for a in b.assets.assets if a.id == 'f_filter_facts')
    case = LocomoAdapter(Path('datasets/locomo/data/locomo10_zh.json')).generation_input('conv-30')
    graph = load_frozen_graph('datasets/locomo/snapshots/gvtest_v1/conv-30', case.corpus)
    registry = FunctionRegistry(b, Limits(30000, 2.0, 180000))
    rows = []
    for tag, p in [('archived_real_failure', {'subject': '乔恩', 'fact_type': '',
                                            'date_prefix': '', 'limit': 500}),
                   *_samples(a, graph)]:
        observation = {}
        try:
            registry.call(a.id, p, graph, _observation=observation)
            error = None
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        rows.append({'scenario': tag, 'parameters': p,
                     'error': error, 'observation': observation})
    OUTPUT['numeric_pressure'] = rows


def main():
    OUTPUT['source_hashes_start'] = {str(p): file_digest(p) for p in SOURCE_PATHS}
    asyncio.run(archived_attribution())
    admission_and_lessons()
    numeric_pressure()
    OUTPUT['source_hashes_end'] = {str(p): file_digest(p) for p in SOURCE_PATHS}
    (HERE / 'counterexamples.json').write_text(json.dumps(OUTPUT, ensure_ascii=False, indent=2))
    print(json.dumps({
        'attribution': OUTPUT['archived_attribution'],
        'check_admission': [{k: r[k] for k in ('candidate', 'admission_verdict',
                                              'real_candidate_opinions')}
                            for r in OUTPUT['check_admission']],
        'lesson_verification': {k: v for k, v in OUTPUT['lesson_verification'].items()
                                if k.endswith('verified')},
        'numeric_samples': len(OUTPUT['numeric_pressure']),
        'numeric_failed': [r for r in OUTPUT['numeric_pressure'] if r['error']],
        'source_unchanged': OUTPUT['source_hashes_start'] == OUTPUT['source_hashes_end']
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
