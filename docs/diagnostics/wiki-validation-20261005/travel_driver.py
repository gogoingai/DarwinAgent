import asyncio,json
from pathlib import Path
from datasets.travelplanner.adapter import TravelPlannerAdapter
from datasets.travelplanner.evaluator import TravelPlannerEvaluator
from oak.config import RunConfig
from oak.experiments import ExperimentRunner,AdoptionPolicy
from oak.kernel import TaskSpec
from oak.kernel.registration import load_assets
from oak.llm.settings import load_connection
from oak.runtime.artifacts import atomic_json

async def main():
 root=Path('datasets/travelplanner/runs/wiki_loop_compat_20261005_case0_runtimecontract').resolve()
 task=Path('tasks/travel_planning').resolve();bundle=load_assets(task).export(root/'initial-assets')
 spec=TaskSpec.load(task/'task.yaml',bundle)
 conn=load_connection(Path.cwd(),root/'runtime','LOCOMO')
 conn.thinking_disabled_roles.update({'answer','review','wiki_maintainer'})
 config=RunConfig()
 runner=ExperimentRunner(TravelPlannerAdapter(),lambda client,stage:TravelPlannerEvaluator(stage/'evaluation'),conn,config,
   AdoptionPolicy('final','macro_cs'),root/'train',optimization_mode='wiki',wiki_call_limit=10,
   frozen_files=(task,Path('datasets/travelplanner/adapter.py'),Path('datasets/travelplanner/evaluator.py')))
 atomic_json(root/'plan.json',{'purpose':'generic Wiki loop TravelPlanner compatibility','cases':['train:0'],'rounds':1,'full_question_set':False,'holdsout_executed':False})
 result=await runner.run('train:0',spec,rounds=1,scope=('S','F','C','P'))
 print(json.dumps(result,ensure_ascii=False),flush=True)
asyncio.run(main())
