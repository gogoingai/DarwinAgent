"""Thin assembly: the framework owns bootstrap, both Agents, the optimization loop and the campaign."""
import argparse
import asyncio
from pathlib import Path

from oak.config import RunConfig
from oak.engine import Pipeline
from oak.experiments import (AdoptionPolicy, CampaignController, ExperimentRunner, ExperimentSpec, SelectionPolicy)
from oak.kernel import KernelBundle, TaskSpec
from oak.llm.client import LLMClient
from oak.llm.settings import load_connection
from .adapter import LocomoAdapter
from .evaluator import LocomoEvaluator, AUDITED, LOCK_PATH
from .exports import write

ROOT=Path(__file__).resolve().parents[2]


async def main(args):
    root=Path(args.output).resolve()
    adapter=LocomoAdapter(ROOT/'datasets/locomo/data/locomo10_zh.json')
    spec=TaskSpec.load(ROOT/'tasks/conversation_memory/task.yaml')
    config=RunConfig()
    connection=load_connection(ROOT,root/'runtime','LOCOMO')
    connection.role_tiers['locomo_judge']='strong'
    connection.empty_response_passthrough_roles.add('locomo_judge')
    frozen=[ROOT/'datasets/locomo/adapter.py',ROOT/'datasets/locomo/evaluator.py',ROOT/'datasets/locomo/exports.py',
            ROOT/'datasets/locomo/run.py',ROOT/'datasets/locomo/pipeline',
            ROOT/'datasets/locomo/data/locomo10_zh.json',ROOT/'datasets/locomo/data/locomo10.json',
            ROOT/'datasets/locomo/data/gold_repairs.jsonl',AUDITED,LOCK_PATH,ROOT/'tasks/conversation_memory']
    if args.stop:
        (root/'STOP').write_text('operator stop\n')
        print('stop signal written; the campaign will lock candidates after the current round')
        return
    if args.campaign:
        protocol=ExperimentSpec(train=('conv-26',),validation=('conv-47',),test=('conv-49',),rounds=args.rounds,
            adoption=AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient')),
            selection=SelectionPolicy('original_precise','original_lenient'))
        controller=CampaignController(adapter,lambda client,path:LocomoEvaluator(client,path),connection,config,
            protocol,root,frozen)
        summary=await controller.run(spec,resume=args.resume)
        print(summary['status'])
    elif args.experiment:
        # 采纳口径按会话自适应：conv-26 有修订 gold 走四口径主指标，其余会话原始 gold 严格为主。
        policy=(AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient'))
                if args.case=='conv-26' else
                AdoptionPolicy('original_precise',('original_lenient',)))
        runner=ExperimentRunner(adapter,lambda client,path:LocomoEvaluator(client,path),connection,config,
            policy,root,frozen)
        summary=await runner.run(args.case,spec,rounds=args.rounds,resume=args.resume)
        print(summary['status'])
    else:
        if not args.assets: raise ValueError('Supply a generated bundle or use --experiment/--campaign')
        async with LLMClient(connection) as client:
            result=await Pipeline(client,root/'generation').run(adapter.generation_input(args.case),
                                                              spec.with_bundle(KernelBundle(Path(args.assets))),config)
            write(result,root/'answers.jsonl')
            scores=await LocomoEvaluator(client,root/'evaluation').evaluate(result)
            from oak.runtime.artifacts import atomic_json
            atomic_json(root/'evaluation.json',scores.to_dict())


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--case',default='conv-26')
    p.add_argument('--output',required=True)
    p.add_argument('--assets')
    p.add_argument('--experiment',action='store_true')
    p.add_argument('--campaign',action='store_true')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--rounds',type=int,default=None,
                   help='训练迭代轮数上限；缺省按 ExperimentSpec（None=无限，操作者 --stop 叫停）')
    p.add_argument('--stop',action='store_true',help='写入 STOP 叫停信号：当前轮完成后锁定候选并进入验证/测试')
    asyncio.run(main(p.parse_args()))
