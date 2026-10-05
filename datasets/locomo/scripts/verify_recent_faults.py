"""Read-only historical fault verification against the current frozen admission gate.

Set OAK_HISTORY_ROOT to the original runs directory and OAK_RECENT_VERIFY_ROOT
to a new, empty directory in the isolated worktree. No original run is modified.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.scripts.regress_history import failed_calls
from oak.config import RunConfig
from oak.experiments.admission import AdmissionError
from oak.experiments.runner import ExperimentRunner
from oak.experiments.snapshots import load_frozen_graph, snapshot_digest
from oak.kernel import KernelBundle
from oak.kernel.functions import FunctionRegistry
from oak.operators.sandbox import Limits
from oak.runtime.artifacts import atomic_json, digest

ROUNDS = (('agentic_v15', 'R6'), ('agentic_v15', 'R7'),
          ('agentic_v15', 'R8'), ('agentic_v16', 'R9'),
          ('agentic_v17', 'R10'),
          ('agentic_v17', 'R12'), ('agentic_v18', 'R13'),
          ('agentic_v20', 'R14'))


def verify_round(source, destination, case, graph, snapshot_root):
    candidate = KernelBundle(source / 'candidate' / 'bundle')
    config = RunConfig(**json.loads(
        (source.parent / 'experiment.json').read_text())['config'])
    calls = list(failed_calls(source))
    copied = destination / 'candidate' / 'bundle'
    shutil.copytree(candidate.root, copied)
    isolated = KernelBundle(copied)
    if isolated.version != candidate.version:
        raise ValueError('Copied candidate identity changed')
    registry = FunctionRegistry(isolated, Limits(
        config.function_steps, config.function_timeout_s, config.result_bytes))
    replays = []
    replay_inputs = []
    for qid, asset_id, params, expected in calls:
        asset = registry.functions.get(asset_id, (None, None))[0]
        row = {'question_id': qid, 'asset_id': asset_id,
               'asset_fingerprint': asset.fingerprint if asset else None,
               'input_digest': digest(params), 'expected_error': expected}
        if asset is None:
            row.update(status='unavailable', reason='Asset not in original candidate')
        elif 'semantic_search' in asset.content:
            row.update(status='not_replayed',
                       reason='Remote embedding cannot be reproduced offline')
        else:
            try:
                registry.call(asset_id, params, graph)
                actual = 'unexpected_success'
            except Exception as exc:
                actual = f'{type(exc).__name__}: {exc}'
            row.update(status='matched' if actual == expected else 'mismatched',
                       actual_error=actual)
            replay_inputs.append((case.id, asset_id, params))
        replays.append(row)
    runner = ExperimentRunner(None, None, None, config, None,
                              destination / 'run', snapshot_root=snapshot_root)
    error = None
    try:
        runner._preflight(isolated, SimpleNamespace(retrieval_floor={}),
                          cases=(case,), replay_inputs=replay_inputs)
    except AdmissionError as exc:
        error = str(exc)[:500]
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'[:500]
    report_path = copied.parent / 'admission.json'
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    replay_rows = [r for r in report.get('scenarios', ())
                   if r.get('scenario_id') == 'replay']
    replay_by_input = {r.get('input_ref', '').rsplit(':', 1)[-1]: r
                       for r in replay_rows}
    for row in replays:
        if row['status'] in ('matched', 'mismatched'):
            admitted = replay_by_input.get(row['input_digest'])
            row['admission_status'] = admitted.get('status') if admitted else 'not_executed'
            row['admission_error'] = admitted.get('error') if admitted else None
            row['same_admission_error'] = bool(admitted and
                row['expected_error'].endswith(admitted.get('error') or '\0'))
    tool_questions = {r['question_id'] for r in replays}
    non_tool_faults = []
    for path in sorted((source / 'generation' / case.id / 'answers').glob('*.json')):
        answer = json.loads(path.read_text())['result']
        if answer['status'] == 'execution_error' and answer['question_id'] not in tool_questions:
            non_tool_faults.append({'question_id':answer['question_id'],
                                    'error':answer['error'],
                                    'status':'not_tool_replayed'})
    matched = [r for r in replays if r['status'] == 'matched']
    caught = all(r.get('admission_status') == 'failed'
                 and r.get('same_admission_error') for r in matched)
    return {'source': str(source), 'candidate_version':candidate.version,
            'candidate_fingerprints': {a.id: a.fingerprint for a in candidate.assets.assets},
            'snapshot_digest': snapshot_digest(snapshot_root / case.id),
            'config_digest': digest(config.to_dict()), 'calls':replays,
            'non_tool_faults':non_tool_faults,
            'actual_matched':len(matched), 'actual_mismatched':sum(
                r['status'] == 'mismatched' for r in replays),
            'not_replayed':sum(r['status'] == 'not_replayed' for r in replays),
            'gate_verdict':report.get('verdict', 'missing_report'),
            'gate_report':str(report_path), 'gate_error':error,
            'caught_all_matched_calls':bool(matched) and caught}


def main():
    history = Path(os.environ['OAK_HISTORY_ROOT']).resolve()
    output = Path(os.environ['OAK_RECENT_VERIFY_ROOT']).resolve()
    if output.exists():
        raise ValueError(f'Validation output must be new: {output}')
    if not output.parent.is_dir():
        raise ValueError(f'Validation parent does not exist: {output.parent}')
    output.mkdir()
    dataset = history.parent
    snapshot_root = dataset / 'snapshots' / 'gvtest_v1'
    case = LocomoAdapter(dataset / 'data' / 'locomo10_zh.json').generation_input('conv-26')
    graph = load_frozen_graph(snapshot_root / case.id, case.corpus)
    summary = {'schema_version':1, 'history_root':str(history),
               'snapshot_root':str(snapshot_root), 'rounds':[],
               'scope':'Original R6-R14 fault candidates; later handoff copies excluded',
               'verdict':'incomplete'}
    summary_path = output / 'summary.json'
    atomic_json(summary_path, summary)
    for version, round_name in ROUNDS:
        source = history / version / 'g1' / 'train' / round_name
        target = output / f'{version}_{round_name}'
        target.mkdir()
        try:
            result = verify_round(source, target, case, graph, snapshot_root)
        except Exception as exc:
            result = {'source':str(source), 'gate_verdict':'incomplete',
                      'error':f'{type(exc).__name__}: {exc}'}
        summary['rounds'].append(result)
        atomic_json(summary_path, summary)
        print(f'{version}/{round_name}: gate={result["gate_verdict"]}, '
              f'actual_matched={result.get("actual_matched", 0)}, '
              f'caught={result.get("caught_all_matched_calls", False)}, '
              f'non_tool={len(result.get("non_tool_faults", ())) }', flush=True)
    summary['local_replay_calls'] = sum(r.get('actual_matched', 0)
                                         for r in summary['rounds'])
    summary['local_replay_caught'] = sum(
        sum(c.get('same_admission_error', False) and c.get('admission_status') == 'failed'
            for c in r.get('calls', ())) for r in summary['rounds'])
    summary['limitations'] = [
        'Remote embedding was unavailable; required semantic search trials are incomplete.',
        'Non-tool answer/review failures have no original failed tool parameters to replay.',
        'A historical negative gate does not establish repaired-candidate content or recall.'
    ]
    summary['later_rounds'] = []
    for name in ('R15', 'R16'):
        source = history / 'agentic_v20' / 'g1' / 'train' / name
        decision = json.loads((source / 'decision.json').read_text())
        summary['later_rounds'].append({
            'source':str(source), 'formal_faults':decision['candidate']['generation_faults'],
            'completed':decision['candidate']['completed'],
            'total':decision['candidate']['total'],
            'accepted':decision['accepted'], 'reasons':decision['reasons']})
    summary['verdict'] = 'passed' if all(
        r.get('caught_all_matched_calls') and r.get('actual_mismatched') == 0
        and r.get('not_replayed') == 0 and r.get('gate_verdict') == 'failed'
        for r in summary['rounds']) else 'failed'
    atomic_json(summary_path, summary)
    print(f'Verification: {summary["verdict"]}; report: {summary_path}')
    return 0 if summary['verdict'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
