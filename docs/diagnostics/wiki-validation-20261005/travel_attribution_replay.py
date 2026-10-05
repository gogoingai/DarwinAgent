import asyncio,json
from pathlib import Path
from datasets.travelplanner.adapter import TravelPlannerAdapter
from oak.contracts import RunResult,AnswerResult
from oak.config import RunConfig
from oak.experiments.runner import _wiki_training_evidence
from oak.experiments.wiki import WikiMaintainer
from oak.llm.client import LLMClient
from oak.llm.settings import load_connection
from oak.runtime.artifacts import atomic_json
async def main():
 source=Path('datasets/travelplanner/runs/wiki_loop_compat_20261005_case0_evalfix/train')
 root=Path('datasets/travelplanner/runs/wiki_attribution_replay_20261005_case0_final')
 archive=json.loads((source/'optimization/wiki.json').read_text())
 facts=next(e['facts'] for e in archive['entries'] if e['kind']=='formal' and e['stage']=='B0')
 d=json.loads((source/'B0/generation/train:0/result.json').read_text());d['answers']=tuple(AnswerResult.from_dict(x) for x in d['answers'])
 result=RunResult(**d);case=TravelPlannerAdapter().generation_input('train:0')
 facts.update(_wiki_training_evidence([case],[result]))
 conn=load_connection(Path.cwd(),root/'runtime','LOCOMO');conn.thinking_disabled_roles.add('wiki_maintainer')
 def factory(_):return LLMClient(conn)
 wiki=WikiMaintainer(root,'archived-frozen-container-failure',factory,RunConfig(protocol_attempts=1),limit=1)
 atomic_json(root/'plan.json',{'purpose':'one real maintainer attribution on archived known C fault with final evidence/boundaries; not formal quality rerun','source':str(source.resolve()),'gold_used':False})
 await wiki.record('B0','formal',facts,category='strategy',scope='formal',training_ids=[e['training_id'] for e in facts['training_examples']],infer=True)
 print(json.dumps(wiki._wiki()['entries'][0]['attribution'],ensure_ascii=False))
asyncio.run(main())
