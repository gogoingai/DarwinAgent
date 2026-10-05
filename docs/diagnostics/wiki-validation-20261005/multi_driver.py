import asyncio,dataclasses,json
from pathlib import Path
from oak.kernel import TaskSpec
from oak.experiments import ExperimentRunner,AdoptionPolicy
from oak.experiments.snapshots import load_frozen_graph,attach_vector
from oak.runtime.artifacts import atomic_json
from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.evaluator import LocomoEvaluator
from datasets.locomo.run import connection,arm_config,memory_structure_sample,smoke_judge,SNAPSHOTS,TASK_DIR,frozen_files

async def main():
 root=Path('datasets/locomo/runs/wiki_multiconv_2q_2r_20261005_smoke6').resolve()
 ids=('conv-30','conv-41','conv-42')
 original=LocomoAdapter(Path('datasets/locomo/data/locomo10_zh.json'))
 class Adapter:
  def generation_input(self,cid):
   c=original.generation_input(cid)
   return dataclasses.replace(c,questions=(c.questions[0],c.questions[20]))
 adapter=Adapter();conn=connection(root);conn.max_concurrency=2;conn.fast_max_concurrency=2
 config=dataclasses.replace(arm_config('g1'),concurrency=2)
 graph=load_frozen_graph(SNAPSHOTS/ids[0],adapter.generation_input(ids[0]).corpus);attach_vector(graph,SNAPSHOTS/ids[0])
 atomic_json(root/'plan.json',{'purpose':'Wiki layer on multiple conversations','cases':[{'id':cid,'questions':[q.to_dict() for q in adapter.generation_input(cid).questions]} for cid in ids],'rounds':2,'questions_per_case':2,'full_question_set':False,'official_holdouts_used':False,'wiki_isolated_from_conv26':True,'smoke_question_ids':[0,1,2,3,4,20],'smoke_gate':'original 2/6 threshold','b0_source':'wiki_multiconv_2q_2r_20261005, exact generation checkpoint identity preserved'})
 class SmallRunner(ExperimentRunner):
  async def _smoke_gate(self,cases,spec,candidate=False,questions_per_case=6):
   expanded=[]
   for c in cases:
    whole=original.generation_input(c.id)
    expanded.append(dataclasses.replace(whole,questions=tuple(whole.questions[i] for i in (0,1,2,3,4,20))))
   return await super()._smoke_gate(expanded,spec,candidate=candidate,questions_per_case=6)
 runner=SmallRunner(adapter,lambda client,p:LocomoEvaluator(client,p),conn,config,
   AdoptionPolicy('original_precise',('original_lenient',)),root/'train',frozen_files(),
   snapshot_root=SNAPSHOTS,bootstrap_context=memory_structure_sample(SNAPSHOTS/ids[0]),
   bootstrap_trial_graph=graph,smoke_judge=smoke_judge,optimization_mode='wiki',wiki_call_limit=15)
 summary=await runner.run(ids,TaskSpec.load(TASK_DIR/'task.yaml'),rounds=2,scope=('S','F','C','P'))
 print(json.dumps(summary,ensure_ascii=False),flush=True)
asyncio.run(main())
