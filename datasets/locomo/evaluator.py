"""Independent frozen original/audited gold x lenient/precise evaluation."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from darwinagent.contracts import EvaluationResult
from darwinagent.runtime.artifacts import atomic_json, verify_files
from .pipeline.data import load_conversation
from .pipeline.protocol import aggregate, dual_grade_batch
from .pipeline.experiment import transcript

ROOT=Path(__file__).resolve().parents[2]
LOCK_PATH=ROOT/'datasets/locomo/evaluation_lock.json'
AUDITED=ROOT/'datasets/locomo/runs/experiments/conv26_dual_v4/gold_audited.json'


class LocomoEvaluator:
    def __init__(self,client,work_dir,dataset_path=None,audited_path=AUDITED,concurrency=4,lock_path=None):
        self.client,self.work_dir=client,Path(work_dir)
        self.lock_path=Path(lock_path) if lock_path is not None else LOCK_PATH
        self.dataset_path=Path(dataset_path or ROOT/'datasets/locomo/data/locomo10_zh.json')
        self.audited_path,self.concurrency=Path(audited_path),concurrency

    async def evaluate(self,result,asked=None):
        """asked＝本轮实际出题的 question idx 集合（允许非连续题号子集）。
        None＝按全会话完整性要求（历史行为，验证/测试/外测全量路径不变）。
        判题上下文永远是全量转写，评分原语不变。"""
        verify_files(ROOT,json.loads(self.lock_path.read_text()))
        conv=load_conversation(self.dataset_path,result.case_id)
        en=load_conversation(ROOT/'datasets/locomo/data/locomo10.json',result.case_id)
        context=transcript(conv)+'\n【英文原句对照】\n'+transcript(en)
        predictions={int(a.question_id):a for a in result.answers}
        asked_ids=None if asked is None else {int(x) for x in asked}
        qas=conv.qas if asked_ids is None else [q for q in conv.qas if q.idx in asked_ids]
        if set(predictions)!={q.idx for q in qas}: raise ValueError('Complete independent answer set required')
        by_idx={q.idx:q for q in qas}
        # 修订 gold 只在 conv-26 存在（审计参考按会话登记）；其余会话按原始 gold 两口径评分。
        audited=None;disputed=set()
        if result.case_id=='conv-26':
            if not self.audited_path.is_file():
                raise FileNotFoundError(f'Audited conv-26 reference missing: {self.audited_path}; pass audited_path explicitly. Audited gold is external evidence and is never fabricated.')
            audited=json.loads(self.audited_path.read_text())
            if len(audited)!=len(conv.qas) or any(row['idx']!=q.idx or row['question']!=q.question for row,q in zip(audited,conv.qas)):
                raise ValueError('Audited reference identity mismatch')
            disputed={row['idx'] for row in audited if row['disputed']}
        golds=[('original',qas)]
        if audited is not None:
            repaired_by_idx={q.idx:replace(q,answer=row['answer'])
                             for row,q in zip(audited,conv.qas)}
            golds.append(('repaired',[repaired_by_idx[q.idx] for q in qas]))
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
        grades_by_idx={gold:{row['idx']:row for row in report['grades']}
                       for gold,report in reports.items()}
        if any(set(rows)!=set(predictions) for rows in grades_by_idx.values()):
            raise ValueError('Incomplete or mismatched independent grade set')
        diagnostics=[]
        for idx in sorted(predictions):
            a=predictions[idx]
            row={'question_id':str(idx),'question':by_idx[idx].question,'status':a.status,'answer':a.answer,
                'error':a.error,'original':grades_by_idx['original'][idx]}
            if audited is not None:
                row['repaired']=grades_by_idx['repaired'][idx]
            diagnostics.append(row)
        metrics={f'{gold}_{metric}':reports[gold]['overall'][metric]['correct']
                 for gold in reports for metric in ('lenient','precise')}
        gen_faults=sum(a.status=='execution_error' for a in result.answers)
        eval_faults=sum(r['status']=='evaluation_error' for report in reports.values() for r in report['grades'])
        completed=sum(all(grades_by_idx[g][idx]['status']=='ok' for g in reports) for idx in predictions)
        verify_files(ROOT,json.loads(self.lock_path.read_text()))
        return EvaluationResult(metrics,len(predictions),completed,gen_faults,eval_faults,tuple(diagnostics))
