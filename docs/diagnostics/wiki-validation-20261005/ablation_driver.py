import asyncio,dataclasses,json,copy
from pathlib import Path
from dotenv import load_dotenv
from oak.kernel import KernelBundle,TaskSpec
from oak.kernel.functions import FunctionRegistry
from oak.kernel.revision import AssetRevisionService,training_id
from oak.kernel.validation import capability_names
from oak.experiments.proposal import ProposalGenerator
from oak.experiments.wiki import _context_facts
from oak.experiments.admission import admit_candidate
from oak.operators.sandbox import Limits
from oak.llm.client import LLMClient
from oak.runtime.artifacts import atomic_json
from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.run import connection,arm_config,bootstrap_trial_graph,TASK_DIR

async def main():
 load_dotenv('.env')
 run=Path('datasets/locomo/runs/wiki_fixed_10q_10r_20261005_05/train')
 root=Path('datasets/locomo/runs/wiki_memory_ablation_20261005').resolve();root.mkdir(parents=True,exist_ok=True)
 base=KernelBundle(run/'B0/assets');on=KernelBundle(run/'R5/candidate/bundle')
 adapter=LocomoAdapter('datasets/locomo/data/locomo10_zh.json');case=adapter.generation_input('conv-26')
 selected={str(i) for i in (0,3,20,32,39,57,65,105,150,180)};case=dataclasses.replace(case,questions=tuple(q for q in case.questions if q.id in selected))
 graph=bootstrap_trial_graph(adapter);config=arm_config('g1');spec=TaskSpec.load(TASK_DIR/'task.yaml')
 wiki=json.loads((run/'optimization/wiki.json').read_text());baseline=next(e for e in wiki['entries'] if e['stage']=='B0' and e['kind']=='formal')
 current=copy.deepcopy(baseline);current['facts']=_context_facts(current['facts']);current.pop('attribution',None)
 context={'version':0,'entries':[current],'lessons':[],
  'objective':{'direction':'基于当前基线训练证据改善答案精确性，可联合修改S/F/C/P；遵守全部契约与预算。'},
  'ablation':'only B0 evidence; no rejected/admitted history and no historical attributions'}
 atomic_json(root/'plan.json',{'purpose':'small behavior ablation, not statistical quality comparison','same_base_version':base.version,'protocol':'trusted refs in both arms','wiki_on':'archived actual R5 candidate','wiki_off':'B0 evidence only, two fresh proposals','probes':'exact R4 failed call and broad admitted-input stress','gold_answers_used':False})
 probes=[{'subject':'卡罗琳','relation':'涉及人物','scan_limit':100,'limit':200},{'subject':'卡罗琳','relation':'涉及人物','scan_limit':500,'limit':300}]
 def measure(label,bundle):
  registry=FunctionRegistry(bundle,Limits(config.function_steps,config.function_timeout_s,config.result_bytes));rows=[]
  for params in probes:
   observation={}
   try:
    result=registry.call('f_relation_expand_facts',params,graph,_observation=observation)
    row={'passed':True,'parameters':params,'observation':observation,'returned_nodes':len(result['node_ids'])}
   except Exception as e:
    row={'passed':False,'parameters':params,'observation':observation,'error':f'{type(e).__name__}: {e}'}
   rows.append(row)
  atomic_json(root/(label+'-probes.json'),rows);print(label,[(r['passed'],r.get('error')) for r in rows],flush=True)
 measure('original-r4',KernelBundle(run/'R4/candidate/bundle'));measure('wiki-on-r5',on);measure('b0',base)
 for index in range(2):
  stage=root/f'current-only-{index}';conn=connection(stage);conn.max_concurrency=1
  try:
   async with LLMClient(conn) as client:
    patches=await ProposalGenerator().propose(base,[case],{},client,config,stage/'proposal-call.json',allowed_kinds=('S','F','C','P'),wiki_context=context)
   bundle=AssetRevisionService().propose(base,patches,stage/'candidate',tuple(training_id(case.id,q.id) for q in case.questions),tuple(q.text for q in case.questions),('S','F','C','P'),capability_names(spec.retrieval_floor))
   measure(f'current-only-{index}',bundle)
   try:admit_candidate(bundle,[case],{case.id:graph},config,capability_names(spec.retrieval_floor),stage/'admission.json')
   except Exception as e:atomic_json(stage/'admission-error.json',{'error':f'{type(e).__name__}: {e}'})
  except Exception as e:
   atomic_json(stage/'failure.json',{'error':f'{type(e).__name__}: {e}'});print('control failure',type(e).__name__,flush=True)
asyncio.run(main())
