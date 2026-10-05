"""Check semantic positives, complete replay binding and compressed production traces.
No model calls; temporary evaluation and candidate directories only.
"""
import copy
import json
import tempfile
from pathlib import Path

from oak.config import RunConfig
from oak.contracts import plain
from oak.experiments.admission import AdmissionError, admit_candidate
from oak.experiments.wiki import _compress_training_evidence, _lessons
from oak.kernel import TaskSpec
from tests.integration.test_wiki_faults_repro import (
    TASK_YAML, IDEAL_C, FIXED_F, base_bundle, candidate_bundle, travel_case, travel_graph,
)

HERE = Path(__file__).resolve().parent


def positive_probe():
    from datasets.travelplanner.adapter import TravelPlannerAdapter
    from datasets.travelplanner.pipeline.config_task import TPConfig
    from datasets.travelplanner.pipeline.data.queries import _from_local
    from datasets.travelplanner.pipeline.eval.adapter import TravelPlannerEvaluator

    spec = TaskSpec.load(TASK_YAML)
    example = plain(spec.answer_examples[0])
    answer = json.loads(example['candidate']['answer'])
    case = travel_case()
    graph = travel_graph(case)
    # Match the official completeness constraint for non-transit days.
    strengthened_c = IDEAL_C.replace(
        "    numbers = [d.get('days') for d in ans]",
        "    for d in ans:\n"
        "        if 'from ' not in str(d.get('current_city', '')):\n"
        "            if any(d.get(k, '') in ('', '-') for k in ('breakfast', 'lunch', 'dinner')):\n"
        "                issues.append('Meals must be present on non-transit days')\n"
        "    numbers = [d.get('days') for d in ans]")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = TPConfig()
        cfg.work_dir = root / 'official'
        query = next(q for q in _from_local(cfg.data_dir / 'train.queries.jsonl') if q.idx == 0)
        score = TravelPlannerEvaluator(cfg).eval_subset([query], [answer])
        b = candidate_bundle(base_bundle(), {'c_answer_shape': strengthened_c,
                                             'f_flight_pair': FIXED_F}, root / 'candidate')
        report_path = root / 'admission.json'
        try:
            admit_candidate(b, [case], {case.id: graph}, RunConfig(), (), report_path,
                            answer_contract=spec.answer_contract,
                            answer_counterexamples=spec.answer_counterexamples,
                            answer_examples=spec.answer_examples)
        except AdmissionError:
            pass
        report = json.loads(report_path.read_text())
        case1 = TravelPlannerAdapter().generation_input('train:1')
        return {'example': example['name'],
                'official_evaluation': {'n': score.n, 'final_cnt': score.final_cnt,
                                        'macro_cs_cnt': score.macro_cs_cnt,
                                        'per_query': score.per_query},
                'strengthened_C_admission': report['verdict'],
                'rejected_positive_rows': [r for r in report['scenarios']
                                           if r['scenario_id'].startswith('answer_ex')],
                'cross_case_binding': {'next_case': case1.id,
                                       'parameters': plain(case1.questions[0].parameters),
                                       'example_cities': [d['current_city'] for d in answer]}}


def binding_probe():
    from oak.runtime.artifacts import digest
    asset = next(a for a in base_bundle().assets.assets if a.id == 'f_flight_pair')
    old_params = {'org': 'A', 'dest': 'B', 'out_date': '2022-03-01', 'back_date': '2022-03-03'}
    new_params = {**old_params, 'dest': 'C'}
    old_ref = 'train:0:stress:0:' + digest(old_params)
    new_ref = 'train:0:stress:1:' + digest(new_params)
    old = {'id': 'old', 'kind': 'attempt', 'stage': 'R1', 'scope': 'admission',
           'facts': {'status': 'failed', 'admission': {'scenarios': [{
               'asset_id': asset.id, 'scenario_id': 'stress', 'status': 'failed',
               'required': True, 'input_ref': old_ref, 'parameters': old_params,
               'error_type': 'ValueError', 'error': 'tool.result.outbound: expected object'}]}}}
    new = {'id': 'new', 'kind': 'attempt', 'stage': 'R2', 'scope': 'admission',
           'facts': {'status': 'passed', 'asset_changes': [{
               'after': {**asset.to_dict(), 'fingerprint': 'new-fingerprint'}}],
               'verification': {'verdict': 'passed', 'scenarios': [{
                   'asset_id': asset.id, 'scenario_id': 'stress', 'status': 'passed',
                   'required': True, 'input_ref': new_ref, 'parameters': new_params}]}}}
    lessons = _lessons([old, new])
    return {'old_ref': old_ref, 'new_ref': new_ref,
            'different_stress_input_marked_verified': any(
                l['status'] == 'admission_verified' for l in lessons), 'lessons': lessons}


def compression_probe():
    root = Path('datasets/travelplanner/runs/wiki_gap_repair_20261005_loop3/train/optimization/events')
    event = next(json.loads(p.read_text()) for p in root.glob('*.json')
                 if json.loads(p.read_text())['stage'] == 'B0'
                 and json.loads(p.read_text())['kind'] == 'formal')
    actual = event['facts']['training_examples'][0]
    candidate = next(t for t in actual['trace'] if t.get('stage') == 'candidate')
    # Production-shaped nested diagnosis, forced through tier 3 by a small facts budget.
    check = copy.deepcopy(candidate)
    proof = json.loads((HERE / 'counterexamples.json').read_text())
    check['checks'] = copy.deepcopy(proof['check_admission'][1]['real_candidate_opinions'])
    example = {'training_id': actual['training_id'],
               'question_parameters': actual['question_parameters'],
               'generated_answer': actual['generated_answer'], 'trace': [check]}
    facts = {'training_examples': [example], 'asset_changes': []}
    for i in range(10):
        facts['asset_changes'].append({'after': {'id': f'f{i}', 'content': 'x' * 5000}})
    original = copy.deepcopy(facts)
    _compress_training_evidence(facts, budget=5000)
    trace = facts['training_examples'][0]['trace']
    return {'budget': 5000, 'result_chars': len(json.dumps(facts, ensure_ascii=False)),
            'original_candidate_event': original['training_examples'][0]['trace'][0],
            'compressed_trace': trace,
            'nested_checks_preserved': any('checks' in t for t in trace),
            'nested_json_type_preserved': any((t.get('candidate_summary') or {}).get('json_type')
                                             or t.get('json_type') for t in trace)}


if __name__ == '__main__':
    result = {'positive': positive_probe(), 'binding': binding_probe(),
              'compression': compression_probe()}
    (HERE / 'additional_counterexamples.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
