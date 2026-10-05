import asyncio,json
from pathlib import Path
import networkx as nx
from datasets.travelplanner.adapter import TravelPlannerAdapter
from oak.config import RunConfig
from oak.contracts import GraphResult
from oak.kernel import KernelBundle,TaskSpec
from oak.kernel.execution import KernelRuntime
from oak.kernel.revision import AssetRevisionService,training_id
from oak.kernel.validation import capability_names
from oak.kg.graph import load_graph
from oak.experiments.wiki import WikiMaintainer
from oak.experiments.proposal import ProposalGenerator
from oak.llm.client import LLMClient
from oak.llm.settings import load_connection
from oak.runtime.artifacts import atomic_json
async def main():
 source=Path('datasets/travelplanner/runs/wiki_loop_compat_20261005_case0_evalfix/train')
 root=Path('datasets/travelplanner/runs/wiki_attribution_replay_20261005_case0_final')
 case=TravelPlannerAdapter().generation_input('train:0');config=RunConfig(protocol_attempts=2)
 conn=load_connection(Path.cwd(),root/'repair/runtime','LOCOMO')
 wiki=WikiMaintainer(root,'archived-frozen-container-failure',lambda _:LLMClient(conn),RunConfig(protocol_attempts=1),limit=1)
 base=KernelBundle(source/'B0/assets');context=wiki.context();formal=next(e for e in wiki._wiki()['entries'] if e['kind']=='formal')
 context['objective']={'direction':formal['attribution']['action'],'evidence_ids':[formal['id']]}
 spec=TaskSpec.load('tasks/travel_planning/task.yaml')
 await wiki.record('repair','attempt',{'status':'failed','error':'CandidateCheckRejected: JSON array now passes but each frozen JSON object was wrongly rejected as day is not an object. Each day is a read-only mapping. Use local = dict(day) before concrete dict type checks; list/tuple are not mapping types. Do not use unregistered hasattr or MappingProxyType names.','verification':{'verdict':'failed'},'source':'repair/probes.json'},category='runtime',scope='admission')
 bundle=None
 for attempt in (4,5):
  context=wiki.context();context['objective']={'direction':formal['attribution']['action'],'evidence_ids':[formal['id']]}
  stage=root/'repair'/f'attempt-{attempt}'
  try:
   async with LLMClient(conn) as client:
    patches=await ProposalGenerator().propose(base,[case],{},client,config,stage/'proposal-call.json',allowed_kinds=('C',),wiki_context=context)
   bundle=AssetRevisionService().propose(base,patches,stage/'candidate',(training_id(case.id,case.questions[0].id),),(case.questions[0].text,),('C',),capability_names(spec.retrieval_floor))
   break
  except Exception as exc:
   bundle=None
   await wiki.record('repair','attempt',{'status':'failed','error':f'{type(exc).__name__}: {exc}','verification':{'verdict':'failed'}},category='runtime',scope='admission')
   atomic_json(stage/'failure.json',{'error':f'{type(exc).__name__}: {exc}'})
 if bundle is None:raise ValueError('Three C repair attempts rejected; preserved Wiki evidence')
 data=json.loads((source/'B0/generation/train:0/result.json').read_text())
 candidate=next(e['candidate'] for e in data['answers'][0]['trace'] if e.get('stage')=='candidate')
 graph=GraphResult(nx.freeze(load_graph(source/'B0/generation/train:0/graph.json')),{b.source.id:b for b in case.corpus})
 probes=[]
 for label,b in (('old',base),('new',bundle)):
  runtime=KernelRuntime(b,config)
  _,checks=runtime.check_candidate(case.questions[0],candidate,graph,set(candidate['node_ids']))
  probes.append({'label':label,'checks':checks,'passed':all(c['ok'] for c in checks)})
  malformed={**candidate,'answer':'not a JSON array'}
  _,bad=runtime.check_candidate(case.questions[0],malformed,graph,set(candidate['node_ids']))
  probes.append({'label':label+'-malformed','checks':bad,'rejected':any(not c['ok'] for c in bad)})
 atomic_json(root/'repair/probes-after-runtime-feedback.json',{'purpose':'one real C proposal from final Wiki, same archived candidate/graph; not a scored Travel loop','results':probes})
 print(json.dumps(probes,ensure_ascii=False),flush=True)
asyncio.run(main())
