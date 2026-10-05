"""End-to-end negative admission checks using frozen Asset trial inputs."""
import json
import asyncio
import sys
import tempfile
import unittest
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from oak.config import RunConfig
from oak.agents.protocol import ModelSession, ProtocolError
from oak.contracts import AnswerResult, CaseInput, EvaluationResult, QuestionInput, RunResult, SourceRef
from oak.experiments.admission import AdmissionError, _samples
from oak.experiments.admission_worker import run_isolated
from oak.experiments.bootstrap import AssetBootstrapper
from oak.experiments.runner import (ExperimentRunner, _retry_journal,
                                    _retryable_answer, _settle_reservations,
                                    _prior_failed_tool_params,
                                    stability_metrics)
from oak.experiments.snapshots import attach_vector, load_frozen_graph
from oak.kernel.assets import Asset, KernelAssets
from oak.kernel.functions import FunctionRegistry
from oak.llm.recorded import RecordedClient
from oak.operators.data import DataCapabilities
from oak.operators.sandbox import Limits
from oak.runtime.artifacts import atomic_json
from oak.runtime.artifacts import digest
from tests.integration.test_agentic_round import (FakeEmbedder, build_snapshot,
                                                   cold_bundle, corpus, gvtest_graph)



class FrozenCandidateAdmissionTests(unittest.TestCase):
    @staticmethod
    def _preflight_sync(runner,*args,**kwargs):
        # _preflight 自动态图准入改造起为协程（见 runner._dynamic_trial_graphs）；
        # 快照/试验图路径内部全同步，asyncio.run 直排即可。
        return asyncio.run(runner._preflight(*args,**kwargs))

    def test_missing_remote_vector_keeps_local_replay_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            seed=cold_bundle(root/'seed',with_c=False)
            local=Asset('f_bad_local','F',
                "def run(params):\n"
                " return [str(row['source_ids']) for row in nodes('原子事实',limit=2)]\n",
                {'type':'object','properties':{}},{'type':'array'},
                ['schema'],trial_inputs=({},))
            bundle=KernelAssets(seed.assets.assets+(local,)).export(root/'candidate')
            case=CaseInput('conv-x',corpus(),(QuestionInput('q1','有哪些事实？'),))
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'run',snapshot_root=snapshot.parent)
            with mock.patch.dict('os.environ',{'EMBEDDING_BASE_URL':'',
                'EMBEDDING_API_KEY':'','EMBEDDING_MODEL':''}):
                with self.assertRaises(AdmissionError):
                    self._preflight_sync(runner,bundle,SimpleNamespace(retrieval_floor={}),
                        cases=(case,),replay_inputs=(('conv-x','f_bad_local',{}),))
            report=json.loads((root/'admission.json').read_text())
            self.assertEqual(report['verdict'],'failed')
            self.assertTrue(any(row['scenario_id']=='remote_vector'
                and row['status']=='incomplete' for row in report['scenarios']))
            self.assertTrue(any(row['scenario_id']=='replay'
                and row['asset_id']=='f_bad_local' and row['status']=='failed'
                and 'Container-to-string' in row['error']
                for row in report['scenarios']))
            self.assertGreater(report['counts']['conv-x']['f_bad_local']['executed'],0)

    def test_model_session_does_not_reclassify_client_value_error(self):
        class Client:
            calls=0

            async def chat(self,**_kwargs):
                self.calls+=1
                raise ValueError('client configuration invalid')

        client=Client()
        session=ModelSession(client,RunConfig(protocol_attempts=2),'test')
        with self.assertRaisesRegex(ValueError,'client configuration invalid'):
            asyncio.run(session.request('answer','system',{},lambda value:value))
        self.assertEqual(client.calls,1)
        self.assertEqual(session.events[0]['status'],'transport_or_budget_error')

    def test_model_session_corrects_invalid_model_parameters(self):
        class Client:
            calls=0

            async def chat(self,**_kwargs):
                self.calls+=1
                return SimpleNamespace(content='{"count":%d}' % self.calls)

        def validator(value):
            if value['count']!=2:
                raise ValueError('Invalid model parameter')
            return value

        client=Client()
        session=ModelSession(client,RunConfig(protocol_attempts=2),'test')
        self.assertEqual(asyncio.run(session.request('answer','system',{},validator)),
                         {'count':2})
        self.assertEqual([event['status'] for event in session.events],
                         ['protocol_error','ok'])

    def test_stage_mixed_fault_retries_only_transient_checkpoint(self):
        from oak.experiments import runner as runner_module

        evidence=(SourceRef('m','c','1'),)
        initial=RunResult('trial','identity','version',(
            AnswerResult('q1','execution_error','',
                         error='TransportExhausted: connection'),
            AnswerResult('q2','execution_error','',
                         error='SandboxError: budget exhausted',
                         trace=({'stage':'tool_error'},)),
            AnswerResult('q3','answered','ok',evidence=evidence)),0)
        after=RunResult('trial','identity','version',(
            AnswerResult('q1','answered','ok',evidence=evidence),
            initial.answers[1],initial.answers[2]),0)

        class Client:
            def ledger_summary(self):
                return {'total_calls':2}

            async def aclose(self):
                pass

        class Pipeline:
            calls=0
            answers_dir=None

            def __init__(self,*_args,**_kwargs):
                pass

            async def run(self,*_args):
                type(self).calls+=1
                if type(self).calls==1:
                    return initial
                self_test.assertFalse((self.answers_dir/f'{digest("q1")}.json').exists())
                self_test.assertTrue((self.answers_dir/f'{digest("q2")}.json').exists())
                self_test.assertTrue((self.answers_dir/f'{digest("q3")}.json').exists())
                return after

        class Evaluator:
            async def evaluate(self,_result,asked=None):
                return EvaluationResult({'m':0},2,3,1,0)

        async def no_sleep(_seconds):
            pass

        original_retry=runner_module.batched_fault_retry

        async def fast_retry(*args,**kwargs):
            kwargs['sleep']=no_sleep
            return await original_retry(*args,**kwargs)

        self_test=self
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            Pipeline.answers_dir=root/'R1'/'generation'/'trial'/'answers'
            for qid in ('q1','q2','q3'):
                atomic_json(Pipeline.answers_dir/f'{digest(qid)}.json',{'question_id':qid})
            case=CaseInput('trial',corpus(),tuple(
                QuestionInput(qid,'问题'+qid) for qid in ('q1','q2','q3')))
            runner=ExperimentRunner(None,lambda _client,_path:Evaluator(),None,
                RunConfig(),None,root,client_factory=lambda _stage:Client())
            spec=SimpleNamespace(bundle=SimpleNamespace(version='version'))
            with mock.patch.object(runner_module,'Pipeline',Pipeline), \
                 mock.patch.object(runner_module,'batched_fault_retry',fast_retry):
                asyncio.run(runner._stage('R1',[case],spec))
            self.assertEqual(Pipeline.calls,2)
            retry=json.loads((root/'R1'/'stage.json').read_text())['fault_retries']['trial']
            self.assertEqual(retry['retried'],1)
            self.assertEqual(retry['skipped_deterministic'],1)
            self.assertEqual(retry['still_faulted'],['q2'])
            journal=json.loads((root/'R1'/'fault-retry'/'trial.json').read_text())
            self.assertEqual(set(journal['questions']),{'q1'})
            self.assertEqual(journal['questions']['q1']['state'],'done')

    def test_stage_resume_does_not_reissue_reserved_mixed_fault(self):
        from oak.experiments import runner as runner_module

        class Client:
            def ledger_summary(self):
                return {'total_calls':4}

            async def aclose(self):
                pass

        class Pipeline:
            runs=0

            def __init__(self,*args,**kwargs):
                pass

            async def run(self,*args):
                type(self).runs+=1
                return RunResult('trial','identity','version',(
                    AnswerResult('q1','execution_error','',
                                 error='TransportExhausted: connection'),
                    AnswerResult('q2','answered','ok',
                                 evidence=(SourceRef('m','c','1'),))),0)

        class Evaluator:
            async def evaluate(self,result,asked=None):
                return EvaluationResult({'m':0},2,2,0,0)

        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            case=CaseInput('trial',corpus(),(
                QuestionInput('q1','问题一'),QuestionInput('q2','问题二')))
            journal=root/'R1'/'fault-retry'/'trial.json'
            atomic_json(journal,{'identity':'identity','questions':{
                'q1':{'state':'reserved','attempts':1,
                      'initial_error_type':'TransportExhausted'}}})
            runner=ExperimentRunner(None,lambda client,path:Evaluator(),None,
                RunConfig(),None,root,client_factory=lambda stage:Client())
            spec=SimpleNamespace(bundle=SimpleNamespace(version='version'))
            with mock.patch.object(runner_module,'Pipeline',Pipeline), \
                 mock.patch.object(runner_module,'batched_fault_retry') as retry:
                asyncio.run(runner._stage('R1',[case],spec))
                retry.assert_not_called()
            self.assertEqual(Pipeline.runs,1)
            saved=json.loads(journal.read_text())['questions']['q1']
            self.assertEqual(saved['state'],'done')
            self.assertEqual(saved['attempts'],1)
            self.assertEqual(saved['final_error_type'],'TransportExhausted')

    def test_timeout_preserves_independent_completed_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            payload=root/'request.json'
            report=root/'report.json'
            payload.write_text(json.dumps({'bundle_version':'candidate','cases':[]}))

            def blocks(*args,**kwargs):
                atomic_json(report,{'candidate_version':'candidate',
                    'verdict':'incomplete','scenarios':[
                        {'asset_id':'f_one','status':'failed','error':'known failure'},
                        {'asset_id':'f_two','status':'passed'}]})
                raise subprocess.TimeoutExpired('worker',1)

            with mock.patch('oak.experiments.admission_worker.subprocess.run',
                            side_effect=blocks):
                self.assertFalse(run_isolated(payload,report,1))
            saved=json.loads(report.read_text())
            self.assertEqual(saved['verdict'],'timeout')
            self.assertEqual([r['asset_id'] for r in saved['scenarios']],
                             ['f_one','f_two','bundle'])

    def test_bootstrap_immediate_feedback_uses_frozen_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            second,_=build_snapshot(root/'second',conv='conv-y')
            shutil.copytree(second,snapshot.parent/'conv-y')
            seed=cold_bundle(root/'seed',with_c=False)
            base=next(a for a in seed.assets.assets if a.kind=='F')
            good=replace(base,id='f_nodes',
                content="def run(params):\n return nodes('原子事实',limit=2)\n",
                input_contract={'type':'object','properties':{}},
                output_contract={'type':'array'},trial_inputs=({},))
            bad=replace(good,content="def run(params):\n"
                " return [str(row['source_ids']) for row in nodes('原子事实',limit=2)]\n")
            common=tuple(a for a in seed.assets.assets if a.kind!='F')
            cases=(CaseInput('conv-x',corpus(),(QuestionInput('q1','检索结果是什么？'),)),
                   CaseInput('conv-y',corpus(),(QuestionInput('q2','有哪些相关内容？'),)))
            spec=SimpleNamespace(seed_s='',requirements=(),retrieval_floor={},
                                 declaration=lambda:{'name':'trial'})
            config=RunConfig(function_timeout_s=15,protocol_attempts=1)

            def client_for(function):
                return RecordedClient({'bootstrap':[{'assets':[
                    a.to_dict() for a in common+(function,)]}]})

            target=root/'B0'/'assets'
            bundle=asyncio.run(AssetBootstrapper().initialize(
                cases,spec,client_for(good),config,target,snapshot_root=snapshot.parent))
            self.assertEqual(bundle.get('f_nodes').fingerprint,good.fingerprint)
            rejected=root/'B0-bad'/'assets'
            with self.assertRaises(ProtocolError) as rejected_error:
                asyncio.run(AssetBootstrapper().initialize(
                    cases,spec,client_for(bad),config,rejected,
                    snapshot_root=snapshot.parent))
            self.assertIn('Container-to-string',str(rejected_error.exception))
            self.assertFalse((rejected/'manifest.json').exists())

    def test_missing_frozen_graph_writes_incomplete_report(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            candidate=cold_bundle(root/'candidate',with_c=False)
            case=CaseInput('missing',corpus(),(QuestionInput('q1','事实'),))
            runner=ExperimentRunner(None,None,None,RunConfig(),None,root/'run',
                                    snapshot_root=root/'snapshots')
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),
                                  cases=(case,))
            report=json.loads((root/'candidate'/'admission.json').read_text())
            self.assertEqual(report['verdict'],'failed')
            self.assertEqual(report['scenarios'][0]['scenario_id'],'training_graph')
            self.assertEqual(report['scenarios'][0]['status'],'incomplete')

    def test_zero_exit_without_report_does_not_admit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            payload=root/'request.json'
            report=root/'report.json'
            payload.write_text(json.dumps({'bundle_version':'candidate','cases':[]}))
            self.assertFalse(run_isolated(payload,report,1,
                command=[sys.executable,'-c','pass']))
            saved=json.loads(report.read_text())
            self.assertEqual(saved['verdict'],'failed')
            self.assertEqual(saved['scenarios'][0]['scenario_id'],'worker_report')

    def test_real_frozen_worker_accepts_good_and_keeps_bad_report(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            second_snapshot,_=build_snapshot(root/'second',conv='conv-y')
            shutil.copytree(second_snapshot,snapshot.parent/'conv-y')
            seed=cold_bundle(root/'seed',with_c=False)
            base=next(a for a in seed.assets.assets if a.kind=='F')
            good=replace(base,id='f_nodes',
                content="def run(params):\n return nodes('原子事实',limit=2)\n",
                input_contract={'type':'object','properties':{}},
                output_contract={'type':'array'},trial_inputs=({},))
            common=tuple(a for a in seed.assets.assets if a.kind!='F')
            working=KernelAssets(common+(good,)).export(root/'working')
            broken=replace(good,id='f_bad',
                content="def run(params):\n"
                        " rows=nodes('原子事实',limit=2)\n"
                        " return [str(row['source_ids']) for row in rows]\n")
            failing=KernelAssets(common+(broken,)).export(root/'failing')
            case=CaseInput('conv-x',corpus(),(QuestionInput('q1','事实'),))
            second=CaseInput('conv-y',corpus(),(QuestionInput('q2','事实'),))
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'run',snapshot_root=snapshot.parent)
            healthy=self._preflight_sync(runner,working,SimpleNamespace(retrieval_floor={}),
                                      cases=(case,second))
            self.assertEqual(healthy['verdict'],'passed')
            self.assertGreater(healthy['counts']['conv-x']['f_nodes']['executed'],0)
            self.assertGreater(healthy['counts']['conv-y']['f_nodes']['executed'],0)
            self.assertTrue(any(row['scenario_id']=='invalid_params_rejected'
                and row['status']=='passed' for row in healthy['scenarios']))
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,failing,SimpleNamespace(retrieval_floor={}),
                                  cases=(case,second))
            report=json.loads((root/'admission.json').read_text())
            self.assertEqual(report['candidate_version'],failing.version)
            self.assertTrue(any(row['asset_id']=='f_bad' and row['status']=='failed'
                and 'Container-to-string' in (row.get('error') or '')
                for row in report['scenarios']))
            self.assertEqual(report['snapshot_digests']['conv-x'],
                             healthy['snapshot_digests']['conv-x'])
            self.assertEqual(set(report['snapshot_digests']),{'conv-x','conv-y'})

    def test_failed_worker_preserves_detailed_admission_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            payload=root/'request.json'
            report=root/'report.json'
            payload.write_text(json.dumps({'bundle_version':'candidate','cases':[]}))

            def refused(*args,**kwargs):
                atomic_json(report,{'candidate_version':'candidate',
                    'verdict':'failed','scenarios':[
                        {'asset_id':'f_one','status':'failed'},
                        {'asset_id':'f_two','status':'failed'}]})
                return SimpleNamespace(returncode=1,stderr='AdmissionError')

            with mock.patch('oak.experiments.admission_worker.subprocess.run',
                            side_effect=refused):
                self.assertFalse(run_isolated(payload,report,1))
            rows=json.loads(report.read_text())['scenarios']
            self.assertEqual([row['asset_id'] for row in rows],
                             ['f_one','f_two'])

    def test_answer_check_sees_actual_function_output_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            seed=cold_bundle(root/'seed',with_c=False)
            source=Asset('f_source','F',
                "def run(params):\n"
                " return [{'node_id':r['node_id'],'source_ids':r['source_ids'],"
                "'marker':'from_fn'} for r in nodes('原子事实',limit=2)]\n",
                {'type':'object','properties':{}},
                {'type':'array','items':{'type':'object','additionalProperties':True}},
                ['schema'],trial_inputs=({},))
            check=Asset('c_shape','C',
                "def check(candidate):\n"
                " if not candidate.get('answer'):\n"
                "  return {'ok':False,'issues':['empty']}\n"
                " if candidate.get('evidence') and "
                "candidate['evidence'][0].get('marker')=='from_fn':\n"
                "  return {'ok':False,'issues':['bad shape']}\n"
                " return {'ok':True,'issues':[]}\n",
                stage='answer',schema_dependencies=['schema'])
            candidate=KernelAssets(tuple(a for a in seed.assets.assets if a.kind!='F')
                                   +(source,check)).export(root/'candidate')
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'run',bootstrap_trial_graph=graph)
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),
                                  QuestionInput('q1','事实'))
            report=json.loads((root/'admission.json').read_text())
            self.assertTrue(any(row['scenario_id']=='function_check'
                and row['asset_id']=='c_shape' and row['status']=='failed'
                and row['issues']==['bad shape'] for row in report['scenarios']))

    def test_function_chain_failure_rejects_individually_passing_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            seed=cold_bundle(root/'seed',with_c=False)
            source=Asset('f_source','F',
                "def run(params):\n"
                " return [{'node_id':r['node_id'],'source_ids':r['source_ids'],"
                "'marker':'from_fn'} for r in nodes('原子事实',limit=2)]\n",
                {'type':'object','properties':{}},
                {'type':'array','items':{'type':'object','additionalProperties':True}},
                ['schema'],trial_inputs=({},))
            target=Asset('f_target','F',
                "def run(params):\n"
                " for row in params['rows']:\n"
                "  if row.get('marker')=='from_fn':\n"
                "   return {'rows':[str(row['source_ids'])]}\n"
                " return {'rows':[]}\n",
                {'type':'object','properties':{'rows':{'type':'array'}},
                 'required':['rows']},
                {'type':'object','properties':{'rows':{'type':'array'}},
                 'required':['rows']},
                ['schema'],trial_inputs=({'rows':[]},))
            candidate=KernelAssets(tuple(a for a in seed.assets.assets if a.kind!='F')
                                   +(source,target)).export(root/'candidate')
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'run',bootstrap_trial_graph=graph)
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),
                                  QuestionInput('q1','事实'))
            report=json.loads((root/'admission.json').read_text())
            self.assertTrue(any(row['scenario_id']=='function_chain'
                and row['asset_id']=='f_target' and row['source_asset_id']=='f_source'
                and row['status']=='failed' and 'Container-to-string' in row['error']
                for row in report['scenarios']))
            self.assertTrue(any(row['asset_id']=='f_target'
                and row['scenario_id']=='base' and row['status']=='passed'
                for row in report['scenarios']))

    def test_saved_tool_failure_becomes_next_candidate_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            attach_vector(graph,snapshot,embedder_factory=lambda:FakeEmbedder())
            candidate=cold_bundle(root/'candidate',with_c=False)
            aid=next(a.id for a in candidate.assets.assets if a.kind=='F')
            run=root/'run'
            atomic_json(run/'R1'/'generation'/'trial'/'answers'/'failed.json',
                {'result':{'status':'execution_error','error':'ValueError: old failure',
                            'trace':[{'stage':'tool_error','asset_id':aid,
                                      'parameters':{'query':'打印机'}}]}})
            self.assertEqual(_prior_failed_tool_params(run),
                             [('trial',aid,{'query':'打印机'})])
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,run,bootstrap_trial_graph=graph)
            report=self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),
                QuestionInput('q1','事实'))
            self.assertTrue(any(row['scenario_id']=='replay'
                and row['asset_id']==aid and row['status']=='passed'
                for row in report['scenarios']))

    def test_stability_metrics_do_not_treat_zero_admissions_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertIsNone(stability_metrics(root)['admission_pass_rate'])
            atomic_json(root/'B0'/'admission.json',
                {'verdict':'passed','elapsed_s':1.5,'smoke':{'status':'passed'}})
            atomic_json(root/'R1'/'.candidate-attempt-0'/'admission.json',
                {'verdict':'failed','elapsed_s':0.5})
            atomic_json(root/'R1'/'stage.json',
                {'scores':{'generation_faults':1},'elapsed_s':12})
            atomic_json(root/'R1'/'fault-retry'/'trial.json',
                {'questions':{'q1':{'initial_error_type':'TransportExhausted',
                                    'final_error_type':'TransportExhausted'}}})
            metrics=stability_metrics(root)
            self.assertEqual(metrics['admission_pass_rate'],0.5)
            self.assertEqual(metrics['smoke_passed'],1)
            self.assertEqual(metrics['formal_execution_faults'],1)
            self.assertEqual(metrics['retry_same_class_failures'],1)

    def test_date_object_and_projection_keep_returned_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            seed=cold_bundle(root/'seed',with_c=False)
            base=next(a for a in seed.assets.assets if a.kind=='F')
            function=replace(base,id='f_date_projection',
                content="def run(params):\n"
                        " date=relative_date(params['anchor_iso'],params['expression'])\n"
                        " rows=nodes('原子事实',limit=2)\n"
                        " return {'date':date,'rows':project(rows,['node_id','source_ids'])}\n",
                input_contract={'type':'object','properties':{
                    'anchor_iso':{'type':'string'},'expression':{'type':'string'}},
                    'required':['anchor_iso','expression']},
                output_contract={'type':'object','properties':{
                    'date':{'type':'object','properties':{
                        'anchor':{'type':'string'},'expression':{'type':'string'},
                        'resolved':{'type':'string'},'granularity':{'type':'string'}},
                        'required':['anchor','expression','resolved','granularity']},
                    'rows':{'type':'array','items':{'type':'object','properties':{
                        'node_id':{'type':'string'},'source_ids':{'type':'array','items':{
                            'type':'string'}}},'required':['node_id','source_ids']}}},
                    'required':['date','rows']},
                trial_inputs=({'anchor_iso':'2024-05-08','expression':'上周日'},))
            bundle=KernelAssets(tuple(a for a in seed.assets.assets if a.kind!='F')
                                +(function,)).export(root/'repaired')
            outcome=FunctionRegistry(bundle,Limits(30000,15,180000)).call(
                function.id,{'anchor_iso':'2024-05-08','expression':'上周日'},graph)
            self.assertEqual(set(outcome['data']['date']),
                             {'anchor','expression','resolved','granularity'})
            self.assertTrue(outcome['data']['date']['resolved'])
            self.assertEqual(outcome['data']['date']['anchor'],'2024-05-08')
            self.assertEqual(outcome['node_ids'],
                             sorted(row['node_id'] for row in outcome['data']['rows']))
            self.assertEqual(outcome['node_ids'],outcome['read_node_ids'])
            self.assertTrue(outcome['source_ids'])
            self.assertEqual(outcome['capability_calls']['relative_date'],1)
            self.assertEqual(outcome['capability_calls']['project'],1)
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'new-run',bootstrap_trial_graph=graph)
            report=self._preflight_sync(runner,bundle,SimpleNamespace(retrieval_floor={}),
                QuestionInput('q1','上周日的事实'))
            self.assertEqual(report['verdict'],'passed')
            self.assertTrue(any(row['scenario_id']=='relative_date_object'
                and row['status']=='passed'
                and row['capability_calls'].get('relative_date')==1
                for row in report['scenarios']))

    def test_high_degree_samples_use_public_row_ids(self):
        graph=gvtest_graph()
        start,end=list(graph.nodes)[:2]
        graph.add_edge(start,end,relation='相关')
        view=SimpleNamespace(graph=graph)
        asset=Asset('f_expand','F',
            "def run(params):\n"
            " return traverse(params['rows'][0]['node_id'], '相关', params['direction'])\n",
            {'type':'object','properties':{'rows':{'type':'array'},
             'direction':{'type':'string','enum':['in','out']}},
             'required':['rows','direction']},
            {'type':'array'},['schema'],trial_inputs=({'rows':[],'direction':'out'},))
        by_tag=dict(_samples(asset,view))
        self.assertIn('high_degree_out',by_tag)
        self.assertIn('high_degree_in',by_tag)
        public_ids=set(DataCapabilities(view).rows)
        self.assertIn(by_tag['high_degree_out']['rows'][0]['node_id'],public_ids)
        self.assertIn(by_tag['high_degree_in']['rows'][0]['node_id'],public_ids)
        caps=DataCapabilities(view)
        for direction in ('in','out'):
            params=by_tag['high_degree_'+direction]
            self.assertTrue(caps.traverse(params['rows'][0]['node_id'],'相关',direction))
        parameterized=replace(asset,
            content="def run(params):\n"
                    " relation=params.get('relation','相关')\n"
                    " return traverse(params['rows'][0]['node_id'],relation=relation,"
                    "direction=params['direction'])\n")
        by_tag=dict(_samples(parameterized,view))
        for direction in ('in','out'):
            params=by_tag['high_degree_'+direction]
            self.assertTrue(caps.traverse(params['rows'][0]['node_id'],'相关',direction))

    def test_high_degree_empty_traversal_is_not_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            seed=cold_bundle(root/'seed',with_c=False)
            function=Asset('f_empty','F',
                "def run(params):\n"
                " relation='不存在'+params['direction']\n"
                " return traverse(params['node_id'],relation,params['direction'])\n",
                {'type':'object','properties':{'node_id':{'type':'string'},
                 'direction':{'type':'string','enum':['in','out']}},
                 'required':['node_id','direction']},
                {'type':'array'},['schema'],
                trial_inputs=({'node_id':'n000000','direction':'out'},))
            bundle=KernelAssets(tuple(a for a in seed.assets.assets if a.kind!='F')
                                +(function,)).export(root/'candidate')
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'run',bootstrap_trial_graph=graph)
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,bundle,SimpleNamespace(retrieval_floor={}),
                                  QuestionInput('q1','事实'))
            report=json.loads((root/'admission.json').read_text())
            for direction in ('in','out'):
                self.assertTrue(any(row['scenario_id']=='high_degree_'+direction
                    and row['status']=='incomplete'
                    and row['error_type']=='CoverageGap'
                    and row['traverse_observations'][0]['matched_edges']==0
                    for row in report['scenarios']))

    def test_candidate_smoke_keeps_each_risk_category_when_dates_dominate(self):
        from oak.experiments import runner as runner_module

        class Client:
            async def aclose(self):
                pass

        class Pipeline:
            seen=()

            def __init__(self,*_args,**_kwargs):
                pass

            async def run(self,case,*_args):
                type(self).seen=tuple(q.id for q in case.questions)
                return SimpleNamespace(answers=(AnswerResult('date0','answered','ok',
                    evidence=(SourceRef('m','c','1'),)),))

        questions=tuple(QuestionInput(f'date{i}',f'昨天第{i}次') for i in range(8))
        questions+=(QuestionInput('filter','过滤全部结果'),
                    QuestionInput('traverse','关系遍历'))
        case=CaseInput('trial',corpus(),questions)
        with tempfile.TemporaryDirectory() as td:
            runner=ExperimentRunner(None,None,None,RunConfig(),None,Path(td),
                client_factory=lambda _stage:Client())
            with mock.patch.object(runner_module,'Pipeline',Pipeline):
                self.assertIsNone(asyncio.run(runner._smoke_gate(
                    [case],SimpleNamespace(),candidate=True)))
        self.assertEqual(len(Pipeline.seen),6)
        self.assertEqual(Pipeline.seen[:3],('date0','filter','traverse'))

    def test_candidate_smoke_recovers_transient_but_blocks_tool_failure(self):
        from oak.experiments import runner as runner_module

        @dataclass(frozen=True)
        class Case:
            id: str = 'trial'
            questions: tuple = (QuestionInput('q1','昨天有什么事实'),)

        class Client:
            async def aclose(self):
                pass

        class Pipeline:
            answer=None

            def __init__(self,*args,**kwargs):
                pass

            async def run(self,*args):
                return SimpleNamespace(answers=(self.answer,))

        async def recovered(*args,**kwargs):
            return SimpleNamespace(answers=(SimpleNamespace(
                question_id='q1',status='answered',error=None),)),[]

        with tempfile.TemporaryDirectory() as td:
            runner=ExperimentRunner(None,None,None,RunConfig(),None,Path(td),
                                    client_factory=lambda stage: Client())
            spec=SimpleNamespace()
            Pipeline.answer=SimpleNamespace(question_id='q1',status='execution_error',
                error='TransportExhausted: connection',trace=())
            with mock.patch.object(runner_module,'Pipeline',Pipeline), \
                 mock.patch.object(runner_module,'batched_fault_retry',
                                   side_effect=recovered) as retry:
                self.assertIsNone(asyncio.run(runner._smoke_gate([Case()],spec,candidate=True)))
                self.assertEqual(retry.call_count,1)
            Pipeline.answer=SimpleNamespace(question_id='q1',status='execution_error',
                error='SandboxError: budget exhausted',trace=({'stage':'tool_error'},))
            with mock.patch.object(runner_module,'Pipeline',Pipeline), \
                 mock.patch.object(runner_module,'batched_fault_retry') as retry:
                self.assertIn('候选冒烟执行故障',
                    asyncio.run(runner._smoke_gate([Case()],spec,candidate=True)))
                retry.assert_not_called()

    def test_unmigrated_historical_params_block_admission(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            attach_vector(graph,snapshot,embedder_factory=lambda:FakeEmbedder())
            candidate=cold_bundle(root/'candidate',with_c=False)
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'new-run',bootstrap_trial_graph=graph)
            aid=next(a.id for a in candidate.assets.assets if a.kind=='F')
            healthy=self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),
                                      QuestionInput('q1','事实'))
            self.assertEqual(healthy['verdict'],'passed')
            self.assertTrue(healthy['framework_digest'])
            self.assertTrue(healthy['snapshot_digests']['trial'])
            self.assertGreater(healthy['counts']['trial'][aid]['by_scenario']['stress']['executed'],0)
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),
                                  QuestionInput('q1','事实'),
                                  replay_inputs=((aid,{'unexpected_parameter':1}),))
            report=json.loads((root/'candidate'/'admission.json').read_text())
            self.assertTrue(any(row['scenario_id']=='replay'
                and row['status']=='incomplete' and row['required']
                for row in report['scenarios']))

    def test_mixed_faults_and_reserved_retry_settlement(self):
        transient=SimpleNamespace(question_id='transient',
            status='execution_error',error='TransportExhausted: connection',trace=())
        deterministic=SimpleNamespace(question_id='deterministic',
            status='execution_error',error='SandboxError: budget exhausted',
            trace=({'stage':'tool_error'},))
        self.assertTrue(_retryable_answer(transient))
        self.assertFalse(_retryable_answer(deterministic))
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'retry.json'
            atomic_json(path,{'identity':'run-v1','questions':{
                'transient':{'state':'reserved','attempts':1,
                             'initial_digest':'before'}}})
            result=SimpleNamespace(identity='run-v1',
                answers=(transient,deterministic))
            _settle_reservations(path,result,{'calls':5})
            journal=_retry_journal(path,'run-v1')
            self.assertEqual(journal['questions']['transient']['state'],'done')
            self.assertEqual(journal['questions']['transient']['attempts'],1)
            self.assertEqual(journal['questions']['transient']['consumed_after'],
                             {'calls':5})
            with self.assertRaises(ValueError):
                _retry_journal(path,'run-v2')

    def test_worker_failure_replaces_stale_passed_report(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            payload=root/'request.json'
            report=root/'report.json'
            payload.write_text(json.dumps({'bundle_version':'changed','cases':[]}))
            report.write_text(json.dumps({'verdict':'passed','candidate_version':'old'}))
            self.assertFalse(run_isolated(payload,report,1,
                command=[sys.executable,'-c','raise RuntimeError("worker failed")']))
            row=json.loads(report.read_text())
            self.assertEqual(row['verdict'],'failed')
            self.assertEqual(row['candidate_version'],'changed')
            self.assertEqual(row['scenarios'][0]['scenario_id'],'worker_exit')

    def test_blocked_child_is_killed_and_timeout_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            payload=root/'request.json'
            report=root/'report.json'
            payload.write_text(json.dumps({'bundle_version':'frozen','cases':[{'id':'train'}]}))
            self.assertFalse(run_isolated(payload,report,0.2,
                command=[sys.executable,'-c','import time; time.sleep(10)']))
            row=json.loads(report.read_text())
            self.assertEqual(row['verdict'],'timeout')
            self.assertEqual(row['candidate_version'],'frozen')
            self.assertEqual(row['cases'],['train'])

    def test_bad_function_fails_pressure_and_independent_checks_continue(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            snapshot,_=build_snapshot(root)
            graph=load_frozen_graph(snapshot,corpus())
            attach_vector(graph,snapshot,embedder_factory=lambda:FakeEmbedder())
            baseline=cold_bundle(root/'seed',with_c=False)
            rows_contract={'type':'object','properties':{'rows':{'type':'array'}},
                           'required':['rows']}
            bad=Asset('f_bad','F',
                "def run(params):\n out=[]\n for row in params['rows']:\n  out.append(str(row.get('source_ids')))\n return {'rows':out}\n",
                rows_contract,{'type':'object','properties':{'rows':{'type':'array'}}},
                ['schema'],trial_inputs=({'rows':[]},))
            good=replace(bad,id='f_good',content=(
                "def run(params):\n"
                " return {'rows': project(params['rows'], ['node_id','source_ids'])}\n"))
            assets=[a for a in baseline.assets.assets if a.kind!='F']+[bad,good]
            candidate=KernelAssets(tuple(assets)).export(root/'candidate')
            runner=ExperimentRunner(None,None,None,RunConfig(function_timeout_s=15),
                None,root/'new-run',bootstrap_trial_graph=graph)
            question=QuestionInput('q1','哪些事实')
            with self.assertRaises(AdmissionError):
                self._preflight_sync(runner,candidate,SimpleNamespace(retrieval_floor={}),question,
                                  replay_inputs=(('f_bad',{'rows':list(
                                      DataCapabilities(graph).rows.values())[:1]}),))
            report=json.loads((root/'admission.json').read_text())
            bad_rows=[x for x in report['scenarios'] if x['asset_id']=='f_bad']
            self.assertTrue(any(x['scenario_id']=='stress' and x['status']=='failed'
                                for x in bad_rows))
            self.assertTrue(any(x['scenario_id']=='replay' and x['status']=='failed'
                                for x in bad_rows))
            self.assertTrue(any(x['asset_id']=='f_good' and x['status']=='passed'
                                for x in report['scenarios']))
            self.assertGreater(report['counts']['trial']['f_bad']['stress_legal'],0)
            self.assertGreater(report['counts']['trial']['f_bad']['executed'],1)
