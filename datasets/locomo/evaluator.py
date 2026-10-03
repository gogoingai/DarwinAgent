"""Independent frozen original/audited gold x lenient/precise evaluation."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from oak.contracts import EvaluationResult
from oak.runtime.artifacts import atomic_json, verify_files
from .pipeline.data import load_conversation
from .pipeline.protocol import aggregate, dual_grade_batch
from .pipeline.experiment import transcript

ROOT=Path(__file__).resolve().parents[2]
LOCK_PATH=ROOT/'datasets/locomo/runs/portable_v1/evaluation_frozen.json'
AUDITED=ROOT/'datasets/locomo/runs/experiments/conv26_dual_v4/gold_audited.json'


class LocomoEvaluator:
    def __init__(self,client,work_dir,dataset_path=None,audited_path=AUDITED,concurrency=4):
        self.client,self.work_dir=client,Path(work_dir)
        self.dataset_path=Path(dataset_path or ROOT/'datasets/locomo/data/locomo10_zh.json')
        self.audited_path,self.concurrency=Path(audited_path),concurrency

    async def evaluate(self,result):
        verify_files(ROOT,json.loads(LOCK_PATH.read_text()))
        conv=load_conversation(self.dataset_path,result.case_id)
        en=load_conversation(ROOT/'datasets/locomo/data/locomo10.json',result.case_id)
        context=transcript(conv)+'\n【英文原句对照】\n'+transcript(en)
        predictions={int(a.question_id):a for a in result.answers}
        if set(predictions)!={q.idx for q in conv.qas}: raise ValueError('Complete independent answer set required')
        # 修订 gold 只在 conv-26 存在（审计参考按会话登记）；其余会话按原始 gold 两口径评分。
        audited=None;disputed=set()
        if result.case_id=='conv-26':
            audited=json.loads(self.audited_path.read_text())
            if len(audited)!=len(conv.qas) or any(row['idx']!=q.idx or row['question']!=q.question for row,q in zip(audited,conv.qas)):
                raise ValueError('Audited reference identity mismatch')
            disputed={row['idx'] for row in audited if row['disputed']}
        golds=[('original',conv.qas)]
        if audited is not None:
            golds.append(('repaired',[replace(q,answer=row['answer']) for q,row in zip(conv.qas,audited)]))
        sem=asyncio.Semaphore(self.concurrency)
        async def block(gold_name,qas):
            async with sem:
                items=[(q,predictions[q.idx].answer,'answer_error' if predictions[q.idx].status=='execution_error' else 'ok') for q in qas]
                return await dual_grade_batch(items,self.client,context,self.work_dir/gold_name/'cache')
        reports={}
        for name,qas in golds:
            parts=await asyncio.gather(*(block(name,qas[i:i+4]) for i in range(0,len(qas),4)))
            reports[name]=aggregate([r for p in parts for r in p],disputed)
            atomic_json(self.work_dir/f'{name}.json',reports[name])
        diagnostics=[]
        for idx in sorted(predictions):
            a=predictions[idx]
            row={'question_id':str(idx),'question':conv.qas[idx].question,'status':a.status,'answer':a.answer,
                'error':a.error,'original':reports['original']['grades'][idx]}
            if audited is not None:
                row['repaired']=reports['repaired']['grades'][idx]
            diagnostics.append(row)
        metrics={f'{gold}_{metric}':reports[gold]['overall'][metric]['correct']
                 for gold in reports for metric in ('lenient','precise')}
        gen_faults=sum(a.status=='execution_error' for a in result.answers)
        eval_faults=sum(r['status']=='evaluation_error' for report in reports.values() for r in report['grades'])
        completed=sum(all(reports[g]['grades'][idx]['status']=='ok' for g in reports) for idx in predictions)
        verify_files(ROOT,json.loads(LOCK_PATH.read_text()))
        return EvaluationResult(metrics,len(conv.qas),completed,gen_faults,eval_faults,tuple(diagnostics))
