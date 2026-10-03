"""Bounded bootstrap -> proposal -> validation -> full evaluation -> adoption controller."""
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

    async def _stage(self,name,case,spec):
        self.verify();started=time.time();stage=self.root/name
        client=self._client(name)
        try:
            result=await Pipeline(client,stage/'generation').run(case,spec,self.config)
            scores_path=stage/'evaluation.json'
            if scores_path.exists():
                saved=json.loads(scores_path.read_text())
                if saved['run_identity']!=result.identity or saved['asset_version']!=spec.bundle.version:
                    raise ValueError('Evaluation checkpoint identity mismatch')
                scores=EvaluationResult(**saved['scores'])
            else:
                evaluator=self.evaluator_factory(client,stage/'evaluation')
                scores=await evaluator.evaluate(result)
                atomic_json(scores_path,{'run_identity':result.identity,'asset_version':spec.bundle.version,'scores':scores.to_dict()})
            self.verify()
            summary={'stage':name,'status':'complete' if scores.completed==scores.total and not scores.evaluation_faults else 'failed',
                     'run_identity':result.identity,'asset_version':spec.bundle.version,'scores':scores.to_dict(),
                     'calls':client.ledger_summary(),'elapsed_s':round(time.time()-started,2)}
            atomic_json(stage/'stage.json',summary)
            print(json.dumps({k:summary[k] for k in ('stage','status','asset_version','elapsed_s')},ensure_ascii=False),flush=True)
            return result,scores
        finally: await client.aclose()

    async def run(self,case_id,spec,rounds=2,resume=False,stop_file=None,b0_gate=None):
        """rounds=None iterates until stop_file appears (operator stop) — unbounded training."""
        if rounds is not None and (type(rounds) is not int or rounds<0):
            raise ValueError('Rounds must be a nonnegative integer or None for unbounded iteration')
        self.verify();case=self.adapter.generation_input(case_id)
        self.root.mkdir(parents=True,exist_ok=True)
        declaration={'case_fingerprint':digest(case.to_dict()),'task':spec.declaration(),'config':self.config.to_dict(),
                     'connection':transport_identity(type('Connection',(),{'cfg':self.connection_config})()),
                     'policy':asdict(self.policy),'frozen_files':self.frozen,'rounds':rounds,'seed_assets':[],
                     'source_layers':sorted({b.source.kind for b in case.corpus})}
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
                try: bundle=await AssetBootstrapper().initialize(case,spec,client,self.config,bundle_path)
                finally: await client.aclose()
            result,baseline=await self._stage('B0',case,spec.with_bundle(bundle))
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
                        result,_=await self._stage(name,case,spec.with_bundle(adopted))
                    continue
                candidate_path=stage/'candidate'/'bundle'
                if (candidate_path/'manifest.json').exists(): candidate=KernelBundle(candidate_path)
                else:
                    client=self._client(name)
                    score_data=baseline.to_dict()
                    rows=score_data.pop('diagnostics',[])
                    # Keep the proposal context bounded; no raw-response concatenation or historical feedback.
                    diagnostics=[];used=0
                    for row in rows:
                        if row.get('repaired',{}).get('precise') is True and row.get('original',{}).get('precise') is True: continue
                        compact={k:row.get(k) for k in ('question_id','question','status','answer','error')}
                        for gold in ('original','repaired'):
                            compact[gold]={k:row.get(gold,{}).get(k) for k in ('status','lenient','precise','missing_elements','wrong_elements','precision_issues','reason')}
                        size=len(json.dumps(compact,ensure_ascii=False))
                        if used+size>35000: break
                        diagnostics.append(compact);used+=size
                    feedback={'scores':score_data,'diagnostics':diagnostics,
                              'diagnostic_rows_total':len(rows),'diagnostic_rows_in_proposal':len(diagnostics),
                              'generation_failures':[{'question_id':a.question_id,'error':a.error} for a in result.answers if a.status=='execution_error'],
                              'graph_diagnostics':plain(result.graph_diagnostics)}
                    try:
                        patches=await ProposalGenerator().propose(adopted,case,feedback,client,self.config,stage/'proposal-call.json')
                        candidate=self.revisions.propose(adopted,patches,stage/'candidate',[q.id for q in case.questions],
                                                         [q.text for q in case.questions])
                    except Exception as exc:
                        decision={'accepted':False,'status':'validation_failed','reasons':[f'{type(exc).__name__}: {exc}'],
                                  'base_version':adopted.version,'candidate':None}
                        atomic_json(decision_path,decision);decisions.append(decision)
                        print(json.dumps({'stage':name,**decision},ensure_ascii=False),flush=True)
                        continue
                    finally: await client.aclose()
                candidate_result,candidate_scores=await self._stage(name,case,spec.with_bundle(candidate))
                decision={**self.policy.decide(baseline,candidate_scores),'base_version':adopted.version,'candidate_version':candidate.version}
                atomic_json(decision_path,decision);decisions.append(decision)
                if decision['accepted']:
                    adopted=self.revisions.publish(candidate,self.root/'published',decision)
                    baseline=candidate_scores;result=candidate_result
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
