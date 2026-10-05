"""Isolated actual-graph trials: a blocked native call cannot stall the controller."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from oak.config import RunConfig
from oak.contracts import CorpusBlock, SourceRef, QuestionInput
from oak.kernel import KernelBundle
from oak.runtime.artifacts import atomic_json, digest
from .admission import admit_candidate
from .snapshots import attach_vector, load_frozen_graph, snapshot_digest


def run_isolated(payload_path, report_path, timeout_s, command=None):
    """The parent owns the deadline and persists an explicit failure on worker death."""
    payload_path,report_path=Path(payload_path),Path(report_path)
    # A previous successful report must never authorize a changed or crashed worker.
    report_path.unlink(missing_ok=True)
    request=json.loads(payload_path.read_text())
    command=command or [sys.executable,'-m','oak.experiments.admission_worker',str(payload_path)]
    started=time.monotonic()
    try:
        finished=subprocess.run(command,capture_output=True,text=True,timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            partial=json.loads(report_path.read_text())
        except (OSError,ValueError):
            partial=None
        if not partial or partial.get('candidate_version')!=request['bundle_version']:
            partial={'schema_version':1,'candidate_version':request['bundle_version'],
                     'asset_fingerprints':request.get('asset_fingerprints',{}),
                     'snapshot_digests':request.get('snapshot_digests',{}),
                     'scenarios':[]}
        partial.update(verdict='timeout',elapsed_s=round(time.monotonic()-started,3),
                       cases=[c['id'] for c in request.get('cases',[])],
                       snapshot_root=request.get('snapshot_root'))
        partial['scenarios'].append(
            {'asset_id':'bundle','scenario_id':'worker_timeout',
             'status':'timeout','required':True,'error':'Admission deadline exceeded'})
        atomic_json(report_path,partial)
        return False
    if finished.returncode:
        try:
            existing=json.loads(report_path.read_text())
        except (OSError,ValueError):
            existing=None
        if existing and existing.get('candidate_version')==request['bundle_version'] \
                and existing.get('verdict') in ('failed','incomplete','timeout'):
            return False
        atomic_json(report_path,{'schema_version':1,'verdict':'failed',
            'elapsed_s':round(time.monotonic()-started,3),
            'candidate_version':request['bundle_version'],
            'asset_fingerprints':request.get('asset_fingerprints',{}),
            'snapshot_digests':request.get('snapshot_digests',{}),
            'scenarios':[{'asset_id':'bundle','scenario_id':'worker_exit',
                          'status':'incomplete','required':True,
                          'error':finished.stderr[-1000:]}]})
        return False
    try:
        report=json.loads(report_path.read_text())
    except (OSError,ValueError):
        report=None
    if report and report.get('verdict')=='passed' \
            and report.get('candidate_version')==request['bundle_version'] \
            and report.get('asset_fingerprints')==request.get('asset_fingerprints') \
            and report.get('snapshot_digests')==request.get('snapshot_digests',{}) \
            and report.get('config_digest')==digest(request['config']):
        return True
    if report is None or report.get('verdict')=='passed':
        atomic_json(report_path,{'schema_version':1,'verdict':'failed',
            'elapsed_s':round(time.monotonic()-started,3),
            'candidate_version':request['bundle_version'],
            'asset_fingerprints':request.get('asset_fingerprints',{}),
            'snapshot_digests':request.get('snapshot_digests',{}),
            'scenarios':[{'asset_id':'bundle','scenario_id':'worker_report',
                          'status':'incomplete','required':True,
                          'error':'Missing or mismatched admission report'}]})
    return False


def _case(data):
    corpus=[]
    for item in data['corpus']:
        src=item['source']
        corpus.append(CorpusBlock(SourceRef(src['kind'],src['document_id'],src['location']),
                                  item['text'],item.get('metadata',{})))
    questions=tuple(QuestionInput(q['id'],q['text'],q.get('parameters',{}))
                    for q in data['questions'])
    return SimpleNamespace(id=data['id'],corpus=tuple(corpus),questions=questions)


def main(payload_path):
    request=json.loads(Path(payload_path).read_text())
    bundle=KernelBundle(request['bundle_path'])
    if bundle.version!=request['bundle_version']:
        raise ValueError('Admission worker bundle identity changed')
    cases=[_case(x) for x in request['cases']]
    graphs={}
    remote_vector_error=None
    needs_vector=any(a.kind=='F' and 'semantic_search' in a.content
                     for a in bundle.assets.assets)
    for case in cases:
        snapshot=Path(request['snapshot_root'])/case.id
        if snapshot_digest(snapshot)!=request['snapshot_digests'][case.id]:
            raise ValueError('Admission worker snapshot identity changed')
        graph=load_frozen_graph(snapshot,case.corpus)
        if needs_vector and remote_vector_error is None:
            try:
                attach_vector(graph,snapshot)
            except RuntimeError as exc:
                if 'EMBEDDING_BASE_URL/EMBEDDING_API_KEY/EMBEDDING_MODEL 未配置' not in str(exc):
                    raise
                remote_vector_error=str(exc)
        graphs[case.id]=graph
    admit_candidate(bundle,cases,graphs,RunConfig(**request['config']),
                    tuple(request['required_caps']),request['report_path'],
                    replay_inputs=tuple(request.get('replay_inputs',())),
                    remote_vector_error=remote_vector_error,
                    replay_checks=tuple(request.get('replay_checks',())),
                    answer_counterexamples=tuple(request.get('answer_counterexamples',())),
                    answer_examples=tuple(request.get('answer_examples',())))


if __name__=='__main__':
    main(sys.argv[1])
