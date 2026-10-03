"""Thin assembly: the framework owns bootstrap, both Agents and the optimization loop."""
import argparse
import asyncio
from pathlib import Path

from oak.config import RunConfig
from oak.engine import Pipeline
from oak.experiments import AdoptionPolicy, ExperimentRunner
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
    if args.experiment:
        frozen=[ROOT/'datasets/locomo/adapter.py',ROOT/'datasets/locomo/evaluator.py',ROOT/'datasets/locomo/exports.py',
                ROOT/'datasets/locomo/run.py',ROOT/'datasets/locomo/pipeline',
                ROOT/'datasets/locomo/data/locomo10_zh.json',ROOT/'datasets/locomo/data/locomo10.json',
                ROOT/'datasets/locomo/data/gold_repairs.jsonl',AUDITED,LOCK_PATH,ROOT/'tasks/conversation_memory/task.yaml']
        runner=ExperimentRunner(adapter,lambda client,path:LocomoEvaluator(client,path),connection,config,
            AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient')),
            root,frozen)
        summary=await runner.run(args.case,spec,resume=args.resume)
        print(summary['status'])
    else:
        if not args.assets: raise ValueError('Supply a generated bundle or use --experiment for cold initialization')
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
    p.add_argument('--resume',action='store_true')
    asyncio.run(main(p.parse_args()))
