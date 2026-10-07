"""Thin assembly of the common Pipeline with two dataset interface implementations."""
import argparse
import asyncio
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.engine import Pipeline
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient
from darwinagent.llm.settings import load_legacy_connection as load_connection
from .adapter import TravelPlannerAdapter
from .evaluator import TravelPlannerEvaluator
from .exports import write

ROOT=Path(__file__).resolve().parents[2]


async def main(args):
    root=Path(args.output).resolve()
    task=ROOT/'tasks/travel_planning'
    bundle=load_assets(task).export(root/'assets')
    spec=TaskSpec.load(task/'task.yaml',bundle)
    adapter=TravelPlannerAdapter(args.split)
    connection=load_connection(ROOT,root/'runtime')
    async with LLMClient(connection) as client:
        result=await Pipeline(client,root/'generation').run(adapter.generation_input(str(args.index)),spec,RunConfig())
        write(result,root/'plans.jsonl')
        scores=await TravelPlannerEvaluator(root/'evaluation').evaluate(result)
        from darwinagent.runtime.artifacts import atomic_json
        atomic_json(root/'evaluation.json',scores.to_dict())


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--split',choices=['train','validation'],default='train')
    p.add_argument('--index',type=int,default=0)
    p.add_argument('--output',required=True)
    asyncio.run(main(p.parse_args()))
