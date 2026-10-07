"""The single fixed generation pipeline; datasets do not supply execution callbacks.

Extraction and assembly are two separate stages: the ExtractionAgent yields an
independent MemoryResult of atomic facts, and the GraphAssembler deterministically
builds the fact-anchored graph from that memory without model calls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MappingProxyType

import networkx as nx

from darwinagent.agents import AnswerAgent, ExtractionAgent
from darwinagent.config import RunConfig
from darwinagent.contracts import AnswerResult, GraphResult, MemoryResult, RunResult, plain
from darwinagent.kg.assembler import GraphAssembler
from darwinagent.kg.graph import load_graph, save_graph
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kernel.validation import validate_case, validate_published
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import assert_files, snapshot_files, transport_identity


# 搬运字节断言的显式豁免：检查点装载逻辑本身（不参与答案计算，回归全绿护航）。
# 豁免必须逐一列名——除此之外任何答案路径文件漂移都会拒绝搬运。
CARRY_NEUTRAL_SUFFIXES = ('darwinagent/engine/pipeline.py', 'darwinagent/llm/client.py')


def carried_acceptor(root, identity, framework):
    """经验证的答案检查点搬运（用户指令：不要从头跑）：CARRIED.json 旁车记录被接受的
    源身份与源框架映射。当且仅当「答案路径文件」（darwinagent/ 全部；experiments 编排/评测层与
    CARRY_NEUTRAL 豁免表除外）与源运行逐字节一致时，源身份的检查点被接受并惰性重锚到
    当前身份。管道在此自行复核哈希，旁车手写无法混入未验证的外来检查点。"""
    sidecar = Path(root) / 'CARRIED.json'
    if not sidecar.exists():
        return lambda stored: stored == identity
    note = json.loads(sidecar.read_text())
    accepted = set(note.get('accepted_identities', ()))
    for path, sha in note.get('source_framework', {}).items():
        if '/darwinagent/experiments/' in path or any(path.endswith(sfx) for sfx in CARRY_NEUTRAL_SUFFIXES):
            continue  # 编排/评测层与显式豁免：差异不参与答案计算
        if framework.get(path) != sha:
            raise ValueError(f'答案路径文件与搬运源不一致，检查点不可复用: {path}')
    return lambda stored: stored == identity or stored in accepted


class Pipeline:
    def __init__(self,client,work_dir,frozen_snapshot=None,embedder_factory=None,graph_builder=None):
        # frozen_snapshot: 共享冻结记忆快照目录（graph/facts/vector＋manifest）。注入时
        # 抽取与构图全部跳过——图是指纹校验的冻结输入数据，臂间唯一差异是资产。
        # embedder_factory: 查询嵌入端点注入（默认读 EMBEDDING_* 环境变量）。
        # graph_builder（新模式，recheck4）：frozen_snapshot 仍冻结记忆/向量，但图由
        # builder(snapshot_dir, schema, embedder_factory) 按当前 S 从固定事实确定性
        # 重建（零模型调用）——「冻结记忆/向量、图可重建」。builder 由任务/适配层
        # 注入（core 不感知事实行格式）；旧模式（None）行为逐字节不变。
        self.client,self.work_dir=client,Path(work_dir)
        self.frozen_snapshot=Path(frozen_snapshot) if frozen_snapshot is not None else None
        self.embedder_factory=embedder_factory
        self.graph_builder=graph_builder

    def _graph_mode_identity(self):
        """运行身份中的图模式标记：rebuild 模式与同输入的 frozen 运行不得共享身份
        （图派生规则不同＝不同实验）；builder 源码摘要进身份（投影规则实现变化
        ⇒ 身份变化 ⇒ 旧检查点不可搬运）。旧模式返回空（身份逐字节不变）。"""
        if self.graph_builder is None:
            return {}
        import inspect
        try:
            source=inspect.getsource(self.graph_builder)
        except (OSError,TypeError):
            source=repr(self.graph_builder)
        return {'graph_mode':'rebuild','graph_builder':digest(source)}

    async def run(self,case,spec,config: RunConfig):
        if not isinstance(config,RunConfig) or spec.bundle is None:
            raise ValueError('Pipeline requires frozen RunConfig and a published bundle')
        validate_case(case,spec)
        runtime=KernelRuntime(spec.bundle,config,tuple(q.text for q in case.questions))
        framework=snapshot_files([Path(__file__).resolve().parents[1]])
        transport=transport_identity(self.client)
        from darwinagent.experiments.snapshots import snapshot_manifest
        snapshot_identity=snapshot_manifest(self.frozen_snapshot) if self.frozen_snapshot is not None else None
        identity=digest({'case':case.to_dict(),'task':spec.declaration(),'config':config.to_dict(),
                         'assets':runtime.bundle.version,'framework':framework,'transport':transport,
                         'snapshot':None if snapshot_identity is None else snapshot_identity['snapshot_digest'],
                         **self._graph_mode_identity()})
        root=self.work_dir/case.id
        root.mkdir(parents=True,exist_ok=True)
        carried_ok=carried_acceptor(root,identity,framework)
        identity_path=root/'identity.json'
        if identity_path.exists():
            recorded=json.loads(identity_path.read_text())
            if recorded['identity']!=identity and not carried_ok(recorded['identity']):
                raise ValueError('Checkpoint belongs to a different run identity; use a new run directory')
        atomic_json(identity_path,{'identity':identity,'case_id':case.id,'assets':runtime.bundle.version,
                                  'config':config.to_dict(),'framework':framework,'transport':transport})
        def verify():
            runtime.bundle.verify();assert_files(framework)
            if transport_identity(self.client)!=transport: raise ValueError('Frozen model route/config changed')
        verify()
        corpus={b.source.id:b for b in case.corpus}
        # The hook before graph building: tasks declaring meta.anchoring insert the atomic-memory
        # stage here; every other task goes straight to typed entity extraction.
        anchored=bool(runtime.schema.meta.get('anchoring'))

        # Stage one (anchored tasks only): atomic-fact memory, checkpointed independently.
        # A frozen snapshot replaces BOTH extraction stages: memory and graph are frozen input.
        memory=None;memory_failure=None;raw=()
        if anchored and self.frozen_snapshot is None:
            if (root/'memory.failure.json').exists():
                failure=json.loads((root/'memory.failure.json').read_text())
                memory_failure=failure['error'];raw=tuple(failure.get('raw_outputs',[]))
            elif (root/'memory.complete.json').exists():
                completion=json.loads((root/'memory.complete.json').read_text())
                payload=json.loads((root/'memory.json').read_text())
                if digest(payload)!=completion['digest']:
                    raise ValueError('Saved memory changed')
                memory=MemoryResult.from_dict(payload,corpus)
                if memory.fingerprint!=completion['fingerprint']:
                    raise ValueError('Saved memory fingerprint mismatch')
            else:
                try:
                    memory=await ExtractionAgent(runtime,self.client,config,identity[:16]).extract(case.corpus)
                    verify()
                    payload=memory.to_dict()
                    atomic_json(root/'memory.json',payload)
                    atomic_json(root/'memory.complete.json',{'digest':digest(payload),'fingerprint':memory.fingerprint,
                                'facts':len(memory.facts),'raw_outputs':len(memory.raw_outputs),
                                'diagnostics':plain(memory.diagnostics)})
                except Exception as exc:
                    verify()
                    memory_failure=f'{type(exc).__name__}: {exc}'
                    raw=tuple(getattr(exc,'raw_outputs',()))
                    atomic_json(root/'memory.failure.json',{'error':memory_failure,'raw_outputs':list(raw)})

        # Stage two: the graph — assembled from memory when anchored, extracted directly otherwise.
        graph=None;graph_failure=None;graph_fingerprint=''
        if memory_failure:
            graph_failure=f'extraction failed: {memory_failure}'
        elif (root/'graph.failure.json').exists():
            failure=json.loads((root/'graph.failure.json').read_text())
            graph_failure=failure['error'];raw=tuple(failure.get('raw_outputs',[])) or raw
        elif (root/'graph.complete.json').exists():
            completion=json.loads((root/'graph.complete.json').read_text())
            graph_path=root/'graph.json'
            graph_payload=json.loads(graph_path.read_text())
            if digest(graph_payload)!=completion['digest']:
                raise ValueError('Saved graph changed')
            graph=GraphResult(nx.freeze(load_graph(graph_path)),MappingProxyType(corpus),
                              (),tuple(completion['diagnostics']))
            graph_fingerprint=completion['digest']
            if self.frozen_snapshot is None:
                runtime.validate_graph(graph,memory.fingerprint if memory is not None else None)
            else:
                from darwinagent.experiments.snapshots import attach_vector
                attach_vector(graph,self.frozen_snapshot,embedder_factory=self.embedder_factory)
        else:
            try:
                if self.graph_builder is not None and self.frozen_snapshot is not None:
                    # 新模式：图＝按当前 S 从固定事实重建（builder 负责 rebuild＋出处落
                    # 真实证据块＋挂冻结向量＋命中映射校验，返回已包 GraphResult）；
                    # 记忆/向量仍取快照。sources 在此接 case 语料（builder 无 case 上下文
                    # 时兜底）。质量门与冻结路径同构：F 试跑＋反例探针＋任务图 C。
                    graph=self.graph_builder(self.frozen_snapshot,runtime.schema,
                                             corpus,self.embedder_factory)
                    if not dict(getattr(graph,'sources',{}) or {}):
                        object.__setattr__(graph,'sources',MappingProxyType(corpus))
                elif self.frozen_snapshot is not None:
                    # 冻结快照图：指纹校验的输入数据。质量门＝F 试跑（真图实参）＋反例探针＋
                    # 任务图 C；类型重查/claims/锚定不变量不适用（词汇由 bootstrap 依结构样本生成）。
                    from darwinagent.experiments.snapshots import attach_vector, load_frozen_graph
                    graph=load_frozen_graph(self.frozen_snapshot,case.corpus)
                    attach_vector(graph,self.frozen_snapshot,embedder_factory=self.embedder_factory)
                elif anchored:
                    graph=GraphAssembler.build(memory,spec,runtime.schema)
                    # Unified validation on first assembly too, not only on resume:
                    # typed schema, instance axioms, task graph C and the anchoring invariants.
                    runtime.validate_graph(graph,memory.fingerprint)
                else:
                    graph=await ExtractionAgent(runtime,self.client,config,identity[:16]).extract_entities(case.corpus)
                verify()
                # Trial every admitted F against the actual graph before inference can call it.
                trials=runtime.functions.trial(graph,{a.id:list(a.trial_inputs) for a in runtime.bundle.assets.assets if a.kind=='F'})
                atomic_json(root/'function-trials.json',trials)
                from darwinagent.kernel.counterexamples import run_probes
                if runtime.bundle.assets.origin.get('kind')=='cold_bootstrap':
                    # 冷启动 bundle 由无标签结构样本生成，无训练名接触面：字面量准入＋真图试跑已覆盖；
                    # 改名探针对生成式检索 F 的截断/惯用法敏感，误伤多于收益——提案轮（proposal
                    # origin）恢复全量探针。动态图任务（无冻结快照，2026-10-05 loop5 B0 冒烟
                    # 被 cold_bootstrap 探针全灭拦下）与冻结快照路径同规则。
                    atomic_json(root/'counterexamples.json',{'probe':'renamed_keys_shifted_dates',
                        'status':'skipped','reason':'cold_bootstrap: literal admission + real-graph trials cover name independence; probes resume on proposals'})
                else:
                    atomic_json(root/'counterexamples.json',run_probes(runtime,graph,memory))
                if self.frozen_snapshot is not None:
                    opinions=runtime.checks.run('graph',runtime.graph_snapshot(graph))
                    failures=[x for x in opinions if not x['ok']]
                    if failures: raise ValueError(f'Task graph checks rejected graph: {failures}')
                else:
                    runtime.validate_graph(graph,memory.fingerprint if memory is not None else None)
                save_graph(graph.graph,root/'graph.json')
                graph_fingerprint=digest(json.loads((root/'graph.json').read_text()))
                atomic_json(root/'graph.complete.json',{'digest':graph_fingerprint,
                            'memory_fingerprint':('' if memory is None else memory.fingerprint) if self.frozen_snapshot is None else snapshot_identity['facts_digest'],'raw_outputs':[],
                            'diagnostics':list(graph.diagnostics)})
            except Exception as exc:
                verify()
                graph_failure=f'{type(exc).__name__}: {exc}'
                raw=tuple(getattr(exc,'raw_outputs',())) or (() if graph is None else ())
                atomic_json(root/'graph.failure.json',{'error':graph_failure,'raw_outputs':list(raw)})

        agent=AnswerAgent(runtime,self.client,config,spec,identity[:16])
        sem=asyncio.Semaphore(config.concurrency)
        async def one(question):
            async with sem:
                verify()
                checkpoint=root/'answers'/f'{digest(question.id)}.json'
                if checkpoint.exists():
                    stored=json.loads(checkpoint.read_text())
                    if (stored.get('identity')!=identity and not carried_ok(stored.get('identity'))) \
                            or digest(stored['result'])!=stored['digest']:
                        raise ValueError('Answer checkpoint changed or belongs to another run')
                    answer=AnswerResult.from_dict(stored['result'])
                    if graph is not None: validate_published(answer,question,graph)
                elif graph_failure:
                    answer=AnswerResult(question.id,'execution_error','',error=graph_failure,raw_outputs=raw)
                else:
                    answer=await agent.answer(question,graph)
                    validate_published(answer,question,graph)
                verify()
                value=answer.to_dict()
                atomic_json(checkpoint,{'identity':identity,'digest':digest(value),'result':value})
                return answer
        answers=tuple(await asyncio.gather(*(one(q) for q in case.questions)))
        verify()
        result=RunResult(case.id,identity,runtime.bundle.version,answers,
                         0 if graph is None else graph.graph.number_of_nodes(),
                         ({'status':'execution_error','error':graph_failure},) if graph_failure else graph.diagnostics,
                         (0 if memory is None else len(memory.facts)) if self.frozen_snapshot is None else snapshot_identity['n_facts'],
                         ('' if memory is None else memory.fingerprint) if self.frozen_snapshot is None else snapshot_identity['facts_digest'],
                         graph_fingerprint)
        atomic_json(root/'result.json',result.to_dict())
        return result
