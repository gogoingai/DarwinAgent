"""Complete dual-metric re-evaluation of one saved answer set, without mixed gold.

python -m datasets.locomo.pipeline.lenient_report conv-26 iter22 [--gold original|audited]
These are local, versioned metrics, not an official LoCoMo evaluator reproduction.
"""
import argparse
import asyncio
import json
from dataclasses import replace

from darwinagent.llm.client import LLMClient
from .config import LOCOMO_TASK_DIR, load_locomo_config
from .data import load_conversation
from .protocol import aggregate, dual_grade_batch


async def amain(conv_id, tag, gold='original'):
    lc=load_locomo_config()
    conv=load_conversation(lc.dataset_path,conv_id)
    out=LOCOMO_TASK_DIR/'runs/dual_reports'/conv_id/tag
    lc.cfg.work_dir=out/'runtime'
    client=LLMClient(lc.cfg)
    answers={o['idx']:o for o in map(json.loads,(lc.conv_dir(conv_id)/tag/'answers.jsonl').read_text().splitlines())}
    disputed=set()
    qas=conv.qas
    if gold=='audited':
        if conv_id!='conv-26':raise ValueError('Audited gold exists only for conv-26')
        rows=json.loads((LOCOMO_TASK_DIR/'runs/experiments/conv26_dual_v4/gold_audited.json').read_text())
        qas=[replace(q,answer=rows[q.idx]['answer']) for q in qas]
        disputed={o['idx'] for o in rows if o['disputed']}
    if set(answers)!=set(range(len(qas))):raise ValueError('Complete answer set required')
    # Import only after initialization to avoid loading experiment configuration.
    from .experiment import transcript
    en=load_conversation(LOCOMO_TASK_DIR/'data/locomo10.json',conv_id)
    source=transcript(conv)+'\n【英文原句对照】\n'+transcript(en)
    sem=asyncio.Semaphore(4)
    async def block(qs):
        items=[(q,answers[q.idx]['answer'],answers[q.idx].get('status','ok')) for q in qs]
        async with sem:
            return await dual_grade_batch(items,client,source,out/'cache')
    grouped=await asyncio.gather(*(block(qas[n:n+4]) for n in range(0,len(qas),4)))
    result=aggregate([r for group in grouped for r in group],disputed)
    out.mkdir(parents=True,exist_ok=True)
    (out/f'{gold}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result['overall'],ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('conv',nargs='?',default='conv-26')
    p.add_argument('tag',nargs='?',default='iter22')
    p.add_argument('--gold',choices=['original','audited'],default='original')
    a=p.parse_args()
    asyncio.run(amain(a.conv,a.tag,a.gold))
