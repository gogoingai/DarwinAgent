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


def training_feedback(results, baseline):
    """Generic proposal feedback from the current training run only. Rows keep the
    evaluator's own diagnostic shape (no dataset field is read by name); rows the
    evaluator marks `passed: true` are skipped; the total stays within the budget."""
    rows = list(plain(baseline.diagnostics))  # 诊断冻结为只读结构，反馈前原样化冻
    picked = []; used = 0
    for row in rows:
        if row.get('passed') is True:
            continue
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size > FEEDBACK_BUDGET_CHARS:
            break
        picked.append(row); used += size
    failures = []
    for result in results:
        failures += [{'question_id': a.question_id, 'error': a.error}
                     for a in result.answers if a.status == 'execution_error']
    graph_diagnostics = []
    for result in results:
        graph_diagnostics += list(result.graph_diagnostics)
    score_data = baseline.to_dict()
    return {'scores': score_data, 'diagnostics': picked,
            'diagnostic_rows_total': len(rows), 'diagnostic_rows_in_proposal': len(picked),
            'generation_failures': failures, 'graph_diagnostics': plain(graph_diagnostics)}


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
                if rounds is not None and n>=rounds: break
                if stop_file is not None and Path(stop_file).exists(): stopped=True; break
                n+=1
                self.verify();name=f'R{n}';stage=self.root/name
                decision_path=stage/'decision.json'
                if decision_path.exists():
                    decision=json.loads(decision_path.read_text());decisions.append(decision)
                    if decision['accepted']:
                        adopted=KernelBundle(stage/'candidate'/'bundle')
                        baseline=EvaluationResult(**decision['candidate'])
                        # Needed as feedback for the next round even when restored.
                        if stage_gate is not None: stage_gate(name)
                        results,_=await self._stage(name,cases,spec.with_bundle(adopted))
                    continue
                candidate_path=stage/'candidate'/'bundle'
                if (candidate_path/'manifest.json').exists(): candidate=KernelBundle(candidate_path)
                else:
                    client=self._client(name)
                    feedback=training_feedback(results,baseline)
                    questions=[]
                    for case in cases:
                        questions+=[{'training_id':q.id,'text':q.text} for q in case.questions]
                    try:
                        patches=await ProposalGenerator().propose(adopted,cases,feedback,client,self.config,stage/'proposal-call.json',questions)
                        training_ids=[q.id for case in cases for q in case.questions]
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
