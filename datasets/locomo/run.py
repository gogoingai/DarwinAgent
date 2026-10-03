"""Thin assembly: the framework owns bootstrap, both Agents, the optimization loop and the campaign.

Agentic round (0.5.0-dev): two arms over ONE frozen memory snapshot per conversation.
  v0 = pure-vector single-shot baseline (deterministic top-K, K tuned on train, rounds=0)
  g1 = agentic graph+vector, cold-start bootstrap from the minimal atomic-memory schema,
       unbounded auto-iteration (operator --stop locks candidates), scope opens P→F→S.
Legacy single-run paths (--assets/--experiment/--campaign without --arm) stay untouched."""
import argparse
import asyncio
import json
from collections import Counter
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
SNAPSHOTS=ROOT/'datasets/locomo/snapshots/gvtest_v1'
TASK_DIR=ROOT/'tasks/conversation_memory'
SCOPE={'p':('P',),'pf':('P','F'),'sfcp':('S','F','C','P')}


def arm_config(arm, vector_k=None):
    # function_timeout_s 放宽：F 内 semantic_search 需要走一次嵌入端点。
    if arm=='v0':
        return RunConfig(retrieval_mode='vector_once',vector_k=vector_k or 30,function_timeout_s=15.0)
    return RunConfig(function_timeout_s=15.0)


def memory_structure_sample(snapshot_dir,max_facts=30):
    """Deterministic, label-free structure sample of the frozen memory plane for cold-start
    bootstrap: node/relation inventories plus a few atomic-memory rows."""
    from oak.kg.graph import load_graph
    manifest=json.loads((Path(snapshot_dir)/'manifest.json').read_text())
    g=load_graph(Path(snapshot_dir)/'graph.json')
    node_types=Counter(nd.get('etype') for _,nd in g.nodes(data=True))
    relations=Counter(ed.get('relation') for _,_,ed in g.edges(data=True))
    facts=[]
    for _,nd in sorted(g.nodes(data=True)):
        if nd.get('etype')=='原子事实' and len(facts)<max_facts:
            facts.append({k:nd.get(k) for k in ('编号','陈述','主体','类型','日期','日期原文','主题','出处')})
    return {'memory_id_field':manifest.get('memory_id_field','编号'),
            'n_facts':manifest['n_facts'],'n_nodes':manifest['n_nodes'],
            'node_types':dict(node_types),'relations':dict(relations),'fact_samples':facts,
            'row_fields':list(dict.fromkeys(
                [k for row in facts for k in row] +
                ['node_id', 'entity_type', 'source_ids', 'claims', 'score']))}


def frozen_files():
    return [ROOT/'datasets/locomo/adapter.py',ROOT/'datasets/locomo/evaluator.py',ROOT/'datasets/locomo/exports.py',
            ROOT/'datasets/locomo/run.py',ROOT/'datasets/locomo/pipeline',
            ROOT/'datasets/locomo/data/locomo10_zh.json',ROOT/'datasets/locomo/data/locomo10.json',
            ROOT/'datasets/locomo/data/gold_repairs.jsonl',AUDITED,LOCK_PATH,TASK_DIR]


def connection(root):
    conn=load_connection(ROOT,root/'runtime','LOCOMO')
    conn.role_tiers['locomo_judge']='strong'
    conn.empty_response_passthrough_roles.add('locomo_judge')
    return conn


def arm_spec(arm,rounds):
    if arm=='v0':
        return ExperimentSpec(train=('conv-26',),validation=('conv-47',),test=('conv-49',),rounds=0,
            adoption=AdoptionPolicy('original_precise',('original_lenient',)),
            selection=SelectionPolicy('original_precise','original_lenient'))
    return ExperimentSpec(train=('conv-26',),validation=('conv-47',),test=('conv-49',),rounds=rounds,
        adoption=AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient')),
        selection=SelectionPolicy('original_precise','original_lenient'))


async def run_arm(args):
    root=Path(args.output).resolve()
    adapter=LocomoAdapter(ROOT/'datasets/locomo/data/locomo10_zh.json')
    task=TaskSpec.load(TASK_DIR/'task.yaml')
    config=arm_config(args.arm,args.vector_k)
    spec=arm_spec(args.arm,args.rounds)
    if args.train_only:
        cases=tuple(c.strip() for c in args.cases.split(',')) if args.cases else spec.train
        runner=ExperimentRunner(adapter,lambda client,path:LocomoEvaluator(client,path),
                                connection(root),config,spec.adoption,root/'train',frozen_files(),
                                snapshot_root=SNAPSHOTS,
                                bootstrap_context=None if args.arm=='v0' else memory_structure_sample(SNAPSHOTS/'conv-26'))
        summary=await runner.run(cases,task,rounds=0,resume=args.resume,scope=SCOPE[args.scope])
        print(summary['status'])
        return
    controller=CampaignController(adapter,lambda client,path:LocomoEvaluator(client,path),
                                  connection(root),config,spec,root,frozen_files(),snapshot_root=SNAPSHOTS)
    if args.arm=='g1':
        controller.bootstrap_context=memory_structure_sample(SNAPSHOTS/'conv-26')
    summary=await controller.run(task,resume=args.resume,scope=SCOPE[args.scope])
    print(summary['status'])


async def main(args):
    root=Path(args.output).resolve()
    if args.stop:
        (root/'STOP').write_text('operator stop\n')
        print('stop signal written; the campaign will lock candidates after the current round')
        return
    if args.arm in ('v0','g1'):
        await run_arm(args)
        return
    adapter=LocomoAdapter(ROOT/'datasets/locomo/data/locomo10_zh.json')
    spec=TaskSpec.load(TASK_DIR/'task.yaml')
    config=RunConfig()
    if args.campaign:
        protocol=ExperimentSpec(train=('conv-26',),validation=('conv-47',),test=('conv-49',),rounds=args.rounds,
            adoption=AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient')),
            selection=SelectionPolicy('original_precise','original_lenient'))
        controller=CampaignController(adapter,lambda client,path:LocomoEvaluator(client,path),connection(root),config,
            protocol,root,frozen_files())
        summary=await controller.run(TaskSpec.load(TASK_DIR/'task.yaml'),resume=args.resume)
        print(summary['status'])
    elif args.experiment:
        policy=(AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient'))
                if args.case=='conv-26' else AdoptionPolicy('original_precise',('original_lenient',)))
        runner=ExperimentRunner(adapter,lambda client,path:LocomoEvaluator(client,path),connection(root),config,
            policy,root,frozen_files(),snapshot_root=SNAPSHOTS if (SNAPSHOTS/args.case/'manifest.json').exists() else None)
        summary=await runner.run(args.case,spec,rounds=args.rounds,resume=args.resume,scope=SCOPE[args.scope])
        print(summary['status'])
    else:
        if not args.assets: raise ValueError('Supply a generated bundle or use --experiment/--campaign/--arm')
        async with LLMClient(connection(root)) as client:
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
    p.add_argument('--arm',choices=('v0','g1'),help='实验臂：v0 纯向量一次检索 / g1 Agentic 图＋向量自动迭代')
    p.add_argument('--vector-k',type=int,default=30,help='v0 臂的检索 K（训练期调优后冻结）')
    p.add_argument('--train-only',action='store_true',help='只跑训练集评测（K 扫描/泛化监测用），不进验证测试')
    p.add_argument('--cases',help='覆盖 case 列表（逗号分隔，train-only 时生效）')
    p.add_argument('--rounds',type=int,default=None,
                   help='迭代轮数上限；v0 固定 0，g1 缺省无限（操作者 --stop 叫停）')
    p.add_argument('--scope',choices=SCOPE.keys(),default='p',
                   help='迭代开放范围：p 只 P / pf 加 F / sfcp 全开（按失败归因推进）')
    p.add_argument('--stop',action='store_true',help='写入 STOP 叫停信号：当前轮完成后锁定候选并进入验证/测试')
    asyncio.run(main(p.parse_args()))
