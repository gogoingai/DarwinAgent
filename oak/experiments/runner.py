"""Bounded bootstrap -> proposal -> validation -> full evaluation -> adoption controller.

Multi-case training: every case of the split runs fully on the same candidate bundle each
round; scores aggregate by the frozen sum rule. Feedback for proposals is dataset-generic:
diagnostic rows travel as the evaluator produced them (opt-out flag `passed: true`),
bounded by a size budget, and only training feedback ever reaches a proposal."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict
from pathlib import Path

from oak.contracts import EvaluationResult, plain
from oak.engine import Pipeline
from oak.kernel import KernelBundle
from oak.kernel.revision import AssetRevisionService
from oak.llm.client import LLMClient
from oak.runtime.artifacts import atomic_json, digest
from oak.runtime.identity import assert_files, snapshot_files, transport_identity
from .bootstrap import AssetBootstrapper
from .proposal import ProposalGenerator
from .spec import aggregate_scores

FEEDBACK_BUDGET_CHARS = 35000


def training_feedback(cases, results, case_diagnostics, baseline):
    """Generic proposal feedback from the current training run only. Question identity is the
    composite (case_id, question_id); the evaluator's diagnostic rows keep their original
    content, tagged with their case. One length budget covers diagnostics, generation
    failures and graph diagnostics together."""
    rows = []
    rows_total = 0
    for case_id, diagnostics in case_diagnostics:
        for row in plain(diagnostics):
            rows_total += 1
            if row.get('passed') is True:
                continue
            rows.append({'case_id': case_id, 'diagnostic': row})
    picked = []; used = 0
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size > FEEDBACK_BUDGET_CHARS:
            break
        picked.append(row); used += size
    failures = []
    for case, result in zip(cases, results):
        failures += [{'case_id': case.id, 'question_id': a.question_id, 'error': a.error}
                     for a in result.answers if a.status == 'execution_error']
    graph_diagnostics = []
    for result in results:
        graph_diagnostics += list(plain(result.graph_diagnostics))
    remaining = FEEDBACK_BUDGET_CHARS - used
    failures_kept = []
    for failure in failures:
        size = len(json.dumps(failure, ensure_ascii=False))
        if remaining - size < 0:
            break
        failures_kept.append(failure); remaining -= size
    graph_kept = []
    for row in graph_diagnostics:
        size = len(json.dumps(row, ensure_ascii=False))
        if remaining - size < 0:
            break
        graph_kept.append(row); remaining -= size
    score_data = baseline.to_dict()
    score_data.pop('diagnostics', None)  # 诊断单独装订，载荷不重复计费
    return {'scores': score_data, 'diagnostics': picked,
            'diagnostic_rows_total': rows_total, 'diagnostic_rows_in_proposal': len(picked),
            'generation_failures': failures_kept,
            'generation_failures_total': len(failures),
            'generation_failures_truncated': len(failures) != len(failures_kept),
            'graph_diagnostics': graph_kept,
            'feedback_budget_chars': FEEDBACK_BUDGET_CHARS}


def question_identity(case):
    """Composite training identity: same-named questions in different cases stay distinct."""
    return [f'{case.id}::{q.id}' for q in case.questions]




def _per_case_feedback_facts(root, name, cases):
    """Per-case diagnostics from the stage's evaluation checkpoints: the aggregated baseline
    loses case attribution, the per-case files keep it."""
    rows = []
    for case in cases:
        path = Path(root) / name / 'evaluation' / f'{case.id}.json'
        diagnostics = ()
        if path.exists():
            diagnostics = plain(json.loads(path.read_text())['scores'].get('diagnostics', ()))
        rows.append((case.id, diagnostics))
    return rows

class ExperimentRunner:
    def __init__(self,adapter,evaluator_factory,connection_config,run_config,policy,work_dir,frozen_files=(),client_factory=None):
        self.adapter,self.evaluator_factory=adapter,evaluator_factory
        self.connection_config,self.config,self.policy=connection_config,run_config,policy
        self.root=Path(work_dir)
        self.frozen=snapshot_files([Path(__file__).resolve().parents[1],*frozen_files])
        self.revisions=AssetRevisionService()
        self._injected_client=client_factory

    def _client(self,stage):
        if self._injected_client is not None:
            return self._injected_client(stage)
        cfg=copy.deepcopy(self.connection_config)
        cfg.work_dir=self.root/stage/'runtime'
        return LLMClient(cfg)

    def verify(self):
        assert_files(self.frozen)

    async def _stage(self,name,cases,spec):
        """Run every case of the split on the same bundle; aggregate by the frozen sum rule."""
        self.verify();started=time.time();stage=self.root/name
        client=self._client(name)
        try:
            results=[];scores=[];identities=[]
            for case in cases:
                result=await Pipeline(client,stage/'generation').run(case,spec,self.config)
                scores_path=stage/'evaluation'/f'{case.id}.json'
                if scores_path.exists():
                    saved=json.loads(scores_path.read_text())
                    if saved['run_identity']!=result.identity or saved['asset_version']!=spec.bundle.version:
                        raise ValueError('Evaluation checkpoint identity mismatch')
                    case_scores=EvaluationResult(**saved['scores'])
                else:
                    evaluator=self.evaluator_factory(client,stage/'evaluation'/case.id)
                    case_scores=await evaluator.evaluate(result)
                    atomic_json(scores_path,{'run_identity':result.identity,'asset_version':spec.bundle.version,
                                             'scores':case_scores.to_dict()})
                results.append(result);scores.append(case_scores);identities.append(result.identity)
            aggregated=aggregate_scores(scores)
            self.verify()
            summary={'stage':name,'cases':[c.id for c in cases],
                     'status':'complete' if aggregated.completed==aggregated.total and not aggregated.evaluation_faults else 'failed',
                     'run_identities':identities,'asset_version':spec.bundle.version,'scores':aggregated.to_dict(),
                     'calls':client.ledger_summary(),'elapsed_s':round(time.time()-started,2)}
            atomic_json(stage/'stage.json',summary)
            print(json.dumps({k:summary[k] for k in ('stage','status','asset_version','elapsed_s')},ensure_ascii=False),flush=True)
            return results,aggregated
        finally: await client.aclose()

    async def run(self,case_ids,spec,rounds=2,resume=False,stop_file=None,b0_gate=None,stage_gate=None):
        """case_ids: one conversation id or a tuple; every case runs fully each round on the
        same candidate bundle. rounds=None iterates until stop_file appears."""
        if isinstance(case_ids,str): case_ids=(case_ids,)
        if not case_ids:
            raise ValueError('Training split needs at least one case')
        if rounds is not None and (type(rounds) is not int or rounds<0):
            raise ValueError('Rounds must be a nonnegative integer or None for unbounded iteration')
        self.verify();cases=[self.adapter.generation_input(c) for c in case_ids]
        self.root.mkdir(parents=True,exist_ok=True)
        declaration={'cases':list(case_ids),'case_fingerprint':digest([c.to_dict() for c in cases]),
                     'aggregation':'sum',
                     'task':spec.declaration(),'config':self.config.to_dict(),
                     'connection':transport_identity(type('Connection',(),{'cfg':self.connection_config})()),
                     'policy':asdict(self.policy),'frozen_files':self.frozen,'rounds':rounds,'seed_assets':[],
                     'source_layers':sorted({b.source.kind for case in cases for b in case.corpus})}
        declaration=json.loads(json.dumps(declaration,ensure_ascii=False))
        experiment_path=self.root/'experiment.json'
        if experiment_path.exists():
            if not resume or json.loads(experiment_path.read_text())!=declaration:
                raise ValueError('Existing experiment requires explicit resume with exactly the same identity')
        else: atomic_json(experiment_path,declaration)
        try:
            bundle_path=self.root/'B0'/'assets'
            if (bundle_path/'manifest.json').exists(): bundle=KernelBundle(bundle_path)
            else:
                client=self._client('B0')
                try: bundle=await AssetBootstrapper().initialize(cases,spec,client,self.config,bundle_path)
                finally: await client.aclose()
            if stage_gate is not None: stage_gate('B0')
            results,baseline=await self._stage('B0',cases,spec.with_bundle(bundle))
            if b0_gate is not None and not b0_gate(baseline):
                summary={'status':'blocked_b0','reason':'baseline gate rejected the B0 evaluation',
                         'baseline':baseline.to_dict(),'rounds':[],'adopted_version':None}
                atomic_json(self.root/'summary.json',summary)
                print(json.dumps({'stage':'B0','status':'blocked_b0'},ensure_ascii=False),flush=True)
                return summary
            adopted=self.revisions.publish(bundle,self.root/'published',{'accepted':True,'reasons':['initial_validated_baseline']})
            decisions=[];stopped=False
            n=0
            while True:
                # Recorded decisions are always restored first — a STOP signal (or a rounds
                # cap) must never truncate history that already happened.
                next_decision=self.root/f'R{n+1}'/'decision.json'
                if next_decision.exists():
                    n+=1
                    self.verify();name=f'R{n}';stage=self.root/name
                    decision=json.loads(next_decision.read_text());decisions.append(decision)
                    if decision['accepted']:
                        adopted=KernelBundle(stage/'candidate'/'bundle')
                        baseline=EvaluationResult(**decision['candidate'])
                        # The publish pointer must follow the restored adoption (B0 was
                        # re-published above during resume), atomically and idempotently.
                        self.revisions.publish(adopted,self.root/'published',decision)
                        # Needed as feedback for the next round even when restored.
                        if stage_gate is not None: stage_gate(name)
                        results,_=await self._stage(name,cases,spec.with_bundle(adopted))
                    continue
                if stop_file is not None and Path(stop_file).exists(): stopped=True; break
                if rounds is not None and n>=rounds: break
                n+=1
                self.verify();name=f'R{n}';stage=self.root/name
                decision_path=stage/'decision.json'
                candidate_path=stage/'candidate'/'bundle'
                if (candidate_path/'manifest.json').exists(): candidate=KernelBundle(candidate_path)
                else:
                    client=self._client(name)
                    feedback=training_feedback(cases,results,_per_case_feedback_facts(self.root,name,cases),baseline)
                    questions=[]
                    for case in cases:
                        questions+=[{'training_id':tid,'text':q.text} for tid,q in zip(question_identity(case),case.questions)]
                    try:
                        patches=await ProposalGenerator().propose(adopted,cases,feedback,client,self.config,stage/'proposal-call.json',questions)
                        training_ids=[tid for case in cases for tid in question_identity(case)]
                        forbidden=[q.text for case in cases for q in case.questions]
                        candidate=self.revisions.propose(adopted,patches,stage/'candidate',training_ids,forbidden)
                    except Exception as exc:
                        decision={'accepted':False,'status':'validation_failed','reasons':[f'{type(exc).__name__}: {exc}'],
                                  'base_version':adopted.version,'candidate':None}
                        atomic_json(decision_path,decision);decisions.append(decision)
                        print(json.dumps({'stage':name,**decision},ensure_ascii=False),flush=True)
                        continue
                    finally: await client.aclose()
                if stage_gate is not None: stage_gate(name)
                candidate_results,candidate_scores=await self._stage(name,cases,spec.with_bundle(candidate))
                decision={**self.policy.decide(baseline,candidate_scores),'base_version':adopted.version,'candidate_version':candidate.version}
                atomic_json(decision_path,decision);decisions.append(decision)
                if decision['accepted']:
                    adopted=self.revisions.publish(candidate,self.root/'published',decision)
                    baseline=candidate_scores;results=candidate_results
                print(json.dumps({'stage':name,'accepted':decision['accepted'],'reasons':decision['reasons']},ensure_ascii=False),flush=True)
            self.verify()
            summary={'status':'complete' if all(d.get('status')!='validation_failed' for d in decisions) else 'failed',
                     'stopped_by_operator':stopped,'rounds':decisions,'adopted_version':adopted.version,
                     'adopted_scores':baseline.to_dict()}
            atomic_json(self.root/'summary.json',summary)
            return summary
        except Exception as exc:
            atomic_json(self.root/'failure.json',{'status':'failed','error':f'{type(exc).__name__}: {exc}'})
            raise
