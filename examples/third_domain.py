"""Offline external-task acceptance. Two interfaces, task assets, the common Pipeline."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import tempfile

from oak.config import RunConfig
from oak.contracts import CaseInput, CorpusBlock, EvaluationResult, QuestionInput, SourceRef
from oak.engine import Pipeline
from oak.kernel import TaskSpec
from oak.kernel.registration import load_assets
from oak.llm.recorded import RecordedClient


class MaintenanceAdapter:
    def generation_input(self,case_id):
        return CaseInput(case_id,(CorpusBlock(SourceRef('maintenance_record',case_id,'row-1'),
            '设备 D-17 于 2026-09-01 由林维护。'),),
            (QuestionInput('q1','谁在什么时候维护了 D-17？',{'serial':'D-17'}),))


class MaintenanceEvaluator:
    async def evaluate(self,result):
        passed=sum(a.status=='answered' and '林' in a.answer and '2026-09-01' in a.answer for a in result.answers)
        return EvaluationResult({'correct':passed},len(result.answers),len(result.answers),
                                sum(a.status=='execution_error' for a in result.answers),0)


async def run(task_root,work_dir):
    case=MaintenanceAdapter().generation_input('device-example')
    bundle=load_assets(task_root).export(work_dir/'assets')
    spec=TaskSpec.load(task_root/'task.yaml',bundle)
    transport=RecordedClient({
        'extraction':[{'entities':[{'type':'Maintenance','key':{'serial':'D-17','date':'2026-09-01'},
                                  'properties':{'technician':'林'},'source_id':case.corpus[0].source.id,'quote':case.corpus[0].text}], 'relations':[]}],
        'tools':[{'action':'call','asset_id':'device_lookup','parameters':{'serial':'D-17'}},{'action':'ready'}],
        'answer':[{'status':'answered','answer':'林于 2026-09-01 维护了设备 D-17。','node_ids':['n000000']}],
        'review':[{'accepted':True,'supported':True,'subject_correct':True,'consistent':True,'complete':True,
                   'abstention_valid':False,'feedback':'记录支持该设备、人员和日期。'}]})
    result=await Pipeline(transport,work_dir/'generation').run(case,spec,RunConfig())
    score=await MaintenanceEvaluator().evaluate(result)
    if score.metrics['correct']!=1: raise RuntimeError(str(result.to_dict()))
    print(result.answers[0].answer)
    print('Common ExtractionAgent + AnswerAgent + Pipeline; offline recorded transport, S/F/C/P executed.')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--task-root',type=Path,default=Path(__file__).resolve().parents[1]/'tasks/device_maintenance')
    p.add_argument('--work-dir',type=Path)
    args=p.parse_args()
    if args.work_dir: asyncio.run(run(args.task_root,args.work_dir))
    else:
        with tempfile.TemporaryDirectory() as td: asyncio.run(run(args.task_root,Path(td)))
