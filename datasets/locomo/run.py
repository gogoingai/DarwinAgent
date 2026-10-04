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


async def smoke_judge(client, case, answers):
    """冒烟判题＝正式判题同一入口（用户：流程为什么不一样）：走 evaluate(asked=抽到的题)，
    与全量轮同一完整性契约、同一判题原语、同一判题器锁校验。旧旁路（dual_grade_batch
    直调）是历史产物——当时 evaluate 硬性要求全会话答案集，3 题子集物理上进不了门；
    asked 语义落地后旁路理由消失。训练集金标对机械门合法可见（conv-26 同时判两口径）。"""
    import tempfile
    from types import SimpleNamespace
    result=SimpleNamespace(case_id=case.id, answers=tuple(answers))
    with tempfile.TemporaryDirectory() as td:
        scores=await LocomoEvaluator(client, Path(td)).evaluate(
            result, asked=tuple(int(a.question_id) for a in answers))
    return {'precise':scores.metrics['original_precise'],
            'completed':scores.total-scores.evaluation_faults,'total':scores.total}


def arm_config(arm, vector_k=None):
    # function_timeout_s 放宽：F 内 semantic_search 需要走一次嵌入端点；protocol_attempts
    # 提到 5：bootstrap 长输出偶发 JSON 手误，多两次反馈重试显著降低换根率。
    if arm=='v0':
        return RunConfig(retrieval_mode='vector_once',vector_k=vector_k or 30,
                         function_timeout_s=15.0,protocol_attempts=5)
    # 并发只改调度不改答案（温度/提示词/判题不变）；glm 池 6 仍低于历史 429 线 8
    return RunConfig(function_timeout_s=15.0,protocol_attempts=5,concurrency=8)


def memory_structure_sample(snapshot_dir,max_facts=30):
    """Deterministic, label-free structure sample of the frozen memory plane for cold-start
    bootstrap: node/relation inventories plus a few atomic-memory rows."""
    from oak.kg.graph import load_graph
    manifest=json.loads((Path(snapshot_dir)/'manifest.json').read_text())
    g=load_graph(Path(snapshot_dir)/'graph.json')
    node_types=Counter(nd.get('etype') for _,nd in g.nodes(data=True))
    relations=Counter(ed.get('relation') for _,_,ed in g.edges(data=True))
    facts=[];sample_row=None
    for _nid,nd in sorted(g.nodes(data=True)):
        if nd.get('etype')=='原子事实' and len(facts)<max_facts:
            facts.append({k:nd.get(k) for k in ('编号','陈述','主体','类型','日期','日期原文','主题','出处')})
    return {'memory_id_field':manifest.get('memory_id_field','编号'),
            'n_facts':manifest['n_facts'],'n_nodes':manifest['n_nodes'],
            'node_types':dict(node_types),'relations':dict(relations),'fact_samples':facts,
            'row_fields':list(dict.fromkeys(
                [k for row in facts for k in row] +
                ['node_id', 'entity_type', 'source_ids', 'claims', 'score'])),
            'row_addressing':'行由运行时字段 node_id（形如 n000001）寻址；编号是记忆业务 id，不是 node_id——'
                             '要遍历关系先用 search/nodes 取行，再把行里的 node_id 传给 traverse。'}


def _trimmed_train_adapter(inner, train_ids, limit):
    """训练集瘦身（用户拍板：迭代提速）：仅训练对话截取前 N 题；验证/测试/外测全量。
    终局对比在持出对话上做，训练集大小是优化参数、不伤两臂公平。"""
    from types import SimpleNamespace
    ids=set(train_ids)
    def generation_input(case_id):
        case = inner.generation_input(case_id)
        if case_id in ids and len(case.questions) > limit:
            import dataclasses
            case = dataclasses.replace(case, questions=tuple(case.questions[:limit]))
        return case
    return SimpleNamespace(generation_input=generation_input)


def frozen_files():
    return [ROOT/'datasets/locomo/adapter.py',ROOT/'datasets/locomo/evaluator.py',ROOT/'datasets/locomo/exports.py',
            ROOT/'datasets/locomo/run.py',ROOT/'datasets/locomo/pipeline',
            ROOT/'datasets/locomo/data/locomo10_zh.json',ROOT/'datasets/locomo/data/locomo10.json',
            ROOT/'datasets/locomo/data/gold_repairs.jsonl',AUDITED,LOCK_PATH,TASK_DIR]


_TRIAL_GRAPH=None
def bootstrap_trial_graph(adapter):
    """冻结 conv-26 快照图（挂向量索引）：冷启动 bootstrap 的反馈环内真图试跑用。
    两臂共用——V0 虽不走工具循环，其 bundle 的 F 仍要在同一记忆面上通过试跑与探针。"""
    global _TRIAL_GRAPH
    if _TRIAL_GRAPH is None:
        from oak.experiments.snapshots import attach_vector, load_frozen_graph
        corpus=adapter.generation_input('conv-26').corpus
        graph=load_frozen_graph(SNAPSHOTS/'conv-26',corpus)
        attach_vector(graph,SNAPSHOTS/'conv-26')
        _TRIAL_GRAPH=graph
    return _TRIAL_GRAPH


def connection(root):
    conn=load_connection(ROOT,root/'runtime','LOCOMO')
    conn.role_tiers['locomo_judge']='strong'
    conn.empty_response_passthrough_roles.add('locomo_judge')
    # 用户决策：全角色关闭深度思考（EmptyCompletion 突发的根因是推理链吃光补全预算）。
    # 「怎么关」按模型走注册表（glm/deepseek 发 thinking:disabled，MiniMax 发
    # reasoning_effort=low，关不掉的模型缓冲兜底）；这里只声明「哪些角色关思考」。
    conn.thinking_disabled_roles.update({'answer','review','locomo_judge'})
    conn.reasoning_effort='low'
    conn.max_concurrency=6
    conn.fast_max_concurrency=8
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
                                bootstrap_context=memory_structure_sample(SNAPSHOTS/'conv-26'),
                                bootstrap_trial_graph=bootstrap_trial_graph(adapter),
                                smoke_judge=smoke_judge)
        summary=await runner.run(cases,task,rounds=0,resume=args.resume,scope=SCOPE[args.scope])
        print(summary['status'])
        return
    if args.train_questions:
        adapter=_trimmed_train_adapter(adapter,spec.train,args.train_questions)
    controller=CampaignController(adapter,lambda client,path:LocomoEvaluator(client,path),
                                  connection(root),config,spec,root,frozen_files(),snapshot_root=SNAPSHOTS,
                                  bootstrap_context=memory_structure_sample(SNAPSHOTS/'conv-26'),
                                  smoke_judge=smoke_judge)
    controller.bootstrap_trial_graph=bootstrap_trial_graph(adapter)
    # 冷启动轮 B0 门＝「可评分基线」：完成度≥90% 即锚定迭代起点（v10：93/100 被旧 95% 门
    # 拦出冷启动死锁——7 题确定性 F 契约故障只有 R1 修资产才能清，而 R1 要 B0 过门才开）。
    # 故障如实进评分与 unhealthy_stages，由采纳门（零故障才可采纳）与迭代清零。
    # 框架默认门（全完+双故障零）不变，仅本轮传入放宽版。
    cold_gate=lambda scores: scores.completed>=max(1,int(scores.total*0.90))
    summary=await controller.run(task,resume=args.resume,scope=SCOPE[args.scope],b0_gate=cold_gate)
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
    p.add_argument('--train-questions',type=int,default=None,
                   help='训练对话截取前 N 题加速迭代（验证/测试/外测保持全量）')
    p.add_argument('--stop',action='store_true',help='写入 STOP 叫停信号：当前轮完成后锁定候选并进入验证/测试')
    asyncio.run(main(p.parse_args()))
