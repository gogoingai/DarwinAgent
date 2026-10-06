"""Offline audit; --apply archives ONLY saved 429 failures for one recovery pass.

Never calls a model, changes assets/config, or removes successful answer/judge caches.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.graph_rules import rebuild_graph, rebuild_snapshot_graph, load_facts
from datasets.locomo.run import ROOT, SNAPSHOTS, TASK_DIR, arm_config, connection
from oak.experiments.snapshots import snapshot_manifest
from oak.kernel import KernelBundle, TaskSpec
from oak.kernel.validation import validate_bundle
from oak.kg.graph import save_graph
from oak.runtime.artifacts import atomic_json, digest
from oak.runtime.identity import snapshot_files, transport_identity

HERE = Path(__file__).resolve().parent
VERSION = '84957c8f4e854c2da7c2dcb00143b47eb44eb1dccaa89d3d83bc4a35ee3be062'
ASSETS = ROOT / 'datasets/locomo/runs/wiki_main_repaired_loop10_20261006_081956/train/published/versions' / VERSION
CASES = ('conv-42', 'conv-43', 'conv-50')
JOURNAL = HERE / 'resume-applied.json'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit():
    bundle = KernelBundle(ASSETS)
    if bundle.version != VERSION:
        raise ValueError('Locked R6 bundle changed')
    task = TaskSpec.load(TASK_DIR / 'task.yaml').with_bundle(bundle)
    adapter = LocomoAdapter(ROOT / 'datasets/locomo/data/locomo10_zh.json')
    framework = snapshot_files([ROOT / 'oak'])
    builder_mode = {'graph_mode': 'rebuild', 'graph_builder': digest(inspect.getsource(rebuild_snapshot_graph))}
    plan = {'assets': str(ASSETS), 'version': VERSION, 'framework': framework,
            'domain_sources': snapshot_files([ROOT / 'datasets/locomo/graph_rules.py',
                                             ROOT / 'datasets/locomo/scripts/external_test.py']),
            'cells': [], 'moves': [], 'graph_rebuild': {}}
    for arm in ('v0', 'g1'):
        root = ROOT / f'datasets/locomo/runs/ext_{arm}_final_20261006'
        for cid in CASES:
            case_root = root / cid
            generation = case_root / 'generation' / cid
            recorded = json.loads((generation / 'identity.json').read_text())
            config = arm_config(arm, 30)
            if recorded['assets'] != VERSION or recorded['config'] != config.to_dict():
                raise ValueError(f'{arm}/{cid}: asset/config mismatch; do not rebase identities')
            if recorded['framework'] != framework:
                raise ValueError(f'{arm}/{cid}: framework changed; do not discard checkpoints')
            transport = transport_identity(SimpleNamespace(cfg=connection(case_root)))
            if recorded['transport'] != transport:
                raise ValueError(f'{arm}/{cid}: model route/credentials/policy changed')
            case = adapter.generation_input(cid)
            snapshot = snapshot_manifest(SNAPSHOTS / cid)
            basis = {'case': case.to_dict(), 'task': task.declaration(), 'config': config.to_dict(),
                     'assets': VERSION, 'framework': framework, 'transport': transport,
                     'snapshot': snapshot['snapshot_digest']}
            matches = [flag for flag in (False, True)
                       if digest({**basis, **(builder_mode if flag else {})}) == recorded['identity']]
            if len(matches) != 1:
                raise ValueError(f'{arm}/{cid}: identity mismatch; no checkpoint mutation allowed')
            mode = matches[0]
            if arm in plan['graph_rebuild'] and plan['graph_rebuild'][arm] != mode:
                raise ValueError(f'{arm}: inconsistent graph modes across cases')
            plan['graph_rebuild'][arm] = mode
            marker = json.loads((generation / 'graph.complete.json').read_text())
            saved_graph = json.loads((generation / 'graph.json').read_text())
            if digest(saved_graph) != marker['digest']:
                raise ValueError(f'{arm}/{cid}: saved graph corrupted')
            if mode:
                facts, _ = load_facts(SNAPSHOTS / cid)
                graph = rebuild_graph(facts, validate_bundle(bundle), case.corpus)
                with tempfile.TemporaryDirectory() as tmp:
                    graph_path = Path(tmp) / 'graph.json'
                    save_graph(graph, graph_path)
                    if digest(json.loads(graph_path.read_text())) != marker['digest']:
                        raise ValueError(f'{arm}/{cid}: current projection differs from saved graph')
            qids = {q.id for q in case.questions}
            seen = set()
            rate_failures, other_failures = [], []
            for path in sorted((generation / 'answers').glob('*.json')):
                cp = json.loads(path.read_text())
                answer = cp['result']
                qid = answer['question_id']
                if (qid not in qids or qid in seen or path.stem != digest(qid)
                        or cp['identity'] != recorded['identity'] or cp['digest'] != digest(answer)):
                    raise ValueError(f'{arm}/{cid}: invalid answer checkpoint {path.name}')
                seen.add(qid)
                if answer['status'] != 'execution_error':
                    continue
                error = str(answer.get('error') or '')
                if error.startswith('TransportExhausted:') and ('429' in error or 'RateLimitError' in error):
                    rate_failures.append(qid)
                    plan['moves'].append({'source': str(path), 'sha256': sha(path),
                                          'arm': arm, 'case_id': cid, 'question_id': qid})
                else:
                    other_failures.append({'question_id': qid, 'error_type': error.split(':', 1)[0]})
            plan['cells'].append({'arm': arm, 'case_id': cid, 'total': len(qids),
                                  'saved': len(seen), 'missing': len(qids - seen),
                                  'rate_failures': sorted(rate_failures, key=int),
                                  'other_failures': other_failures})
    return plan


def apply(plan):
    if JOURNAL.exists():
        saved = json.loads(JOURNAL.read_text())
        if saved.get('status') == 'prepared':
            print('Already prepared; will not reopen failed questions for another retry pass.')
            return
        plan = saved['plan']
        archive = Path(saved['archive'])
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        archive = ROOT / 'datasets/locomo/runs' / f'paused_external_backup_{stamp}'
        saved = {'status': 'preparing', 'archive': str(archive), 'plan': plan}
        atomic_json(JOURNAL, saved)
    # Resume an interrupted preparation only while the exact audited sources still match.
    for path, expected in {**plan['framework'], **plan['domain_sources']}.items():
        if sha(path) != expected:
            raise ValueError(f'Code changed since preparation: {path}')
    for arm in ('v0', 'g1'):
        root = ROOT / f'datasets/locomo/runs/ext_{arm}_final_20261006'
        # Retain original aggregates as well as individual failed checkpoints.
        outputs = [root / 'report.json']
        for cid in CASES:
            outputs += [root / cid / 'scores.json', root / cid / 'evaluation/original.json',
                        root / cid / 'generation' / cid / 'result.json']
        for path in outputs:
            if path.exists():
                target = archive / arm / 'old-outputs' / path.relative_to(root)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
    for move in plan['moves']:
        source = Path(move['source'])
        target = archive / move['arm'] / move['case_id'] / 'answers' / source.name
        if target.exists():
            if sha(target) != move['sha256'] or source.exists():
                raise ValueError(f'Ambiguous partial preparation: {source}')
            continue
        if not source.exists() or sha(source) != move['sha256']:
            raise ValueError(f'Checkpoint changed since audit: {source}')
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    saved['status'] = 'prepared'
    atomic_json(JOURNAL, saved)
    print(f"Archived {len(plan['moves'])} rate-limit failures; no model requests made. Backup: {archive}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Prepare one retry pass after the user resumes')
    args = parser.parse_args()
    if args.apply and JOURNAL.exists() and json.loads(JOURNAL.read_text()).get('status') == 'prepared':
        print('Already prepared; no changes made.')
        return
    plan = audit()
    if not args.apply:
        atomic_json(HERE / 'resume-plan.json', plan)
        for cell in plan['cells']:
            print(f"{cell['arm']}/{cell['case_id']}: saved={cell['saved']}/{cell['total']}, "
                  f"missing={cell['missing']}, 429={len(cell['rate_failures'])}, "
                  f"other_faults={len(cell['other_failures'])}")
        print('graph_rebuild:', plan['graph_rebuild'])
        print('Audit only: no original checkpoint changes or model requests.')
    else:
        apply(plan)


if __name__ == '__main__':
    main()
