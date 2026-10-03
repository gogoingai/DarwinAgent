"""The single fixed generation pipeline; datasets do not supply execution callbacks."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MappingProxyType

import networkx as nx

from oak.agents import AnswerAgent, ExtractionAgent
from oak.config import RunConfig
from oak.contracts import AnswerResult, GraphResult, RunResult
from oak.kernel.execution import KernelRuntime
from oak.kernel.validation import validate_case, validate_published
from oak.kg.graph import load_graph, save_graph
from oak.runtime.artifacts import atomic_json, digest
from oak.runtime.identity import assert_files, snapshot_files, transport_identity


class Pipeline:
    def __init__(self,client,work_dir):
        self.client,self.work_dir=client,Path(work_dir)

    async def run(self,case,spec,config: RunConfig):
        if not isinstance(config,RunConfig) or spec.bundle is None:
            raise ValueError('Pipeline requires frozen RunConfig and a published bundle')
        validate_case(case,spec)
        runtime=KernelRuntime(spec.bundle,config,tuple(q.text for q in case.questions))
        framework=snapshot_files([Path(__file__).resolve().parents[1]])
        transport=transport_identity(self.client)
        identity=digest({'case':case.to_dict(),'task':spec.declaration(),'config':config.to_dict(),
                         'assets':runtime.bundle.version,'framework':framework,'transport':transport})
        root=self.work_dir/case.id
        root.mkdir(parents=True,exist_ok=True)
        identity_path=root/'identity.json'
        if identity_path.exists() and json.loads(identity_path.read_text())['identity']!=identity:
            raise ValueError('Checkpoint belongs to a different run identity; use a new run directory')
        atomic_json(identity_path,{'identity':identity,'case_id':case.id,'assets':runtime.bundle.version,
                                  'config':config.to_dict(),'framework':framework,'transport':transport})
        def verify():
            runtime.bundle.verify();assert_files(framework)
            if transport_identity(self.client)!=transport: raise ValueError('Frozen model route/config changed')
        verify()
        graph=None;graph_failure=None;raw=()
        if (root/'graph.failure.json').exists():
            failure=json.loads((root/'graph.failure.json').read_text())
            graph_failure=failure['error'];raw=tuple(failure.get('raw_outputs',[]))
        elif (root/'graph.complete.json').exists():
            completion=json.loads((root/'graph.complete.json').read_text())
            graph_path=root/'graph.json'
            if digest(json.loads(graph_path.read_text()))!=completion['digest']:
                raise ValueError('Saved graph changed')
            graph=GraphResult(nx.freeze(load_graph(graph_path)),MappingProxyType({b.source.id:b for b in case.corpus}),
                              tuple(completion['raw_outputs']),tuple(completion['diagnostics']))
            runtime.validate_graph(graph)
        else:
            try:
                graph=await ExtractionAgent(runtime,self.client,config,identity[:16]).extract(case.corpus)
                verify()
                # Trial every admitted F against the actual graph before inference can call it.
                trials=runtime.functions.trial(graph,{a.id:list(a.trial_inputs) for a in runtime.bundle.assets.assets if a.kind=='F'})
                atomic_json(root/'function-trials.json',trials)
                from oak.kernel.counterexamples import run_probes
                atomic_json(root/'counterexamples.json',run_probes(runtime,graph))
                save_graph(graph.graph,root/'graph.json')
                atomic_json(root/'graph.complete.json',{'digest':digest(json.loads((root/'graph.json').read_text())),
                            'raw_outputs':list(graph.raw_outputs),'diagnostics':list(graph.diagnostics)})
            except Exception as exc:
                verify()
                graph_failure=f'{type(exc).__name__}: {exc}'
                raw=tuple(getattr(exc,'raw_outputs',())) or (() if graph is None else graph.raw_outputs)
                atomic_json(root/'graph.failure.json',{'error':graph_failure,'raw_outputs':list(raw)})
        agent=AnswerAgent(runtime,self.client,config,spec,identity[:16])
        sem=asyncio.Semaphore(config.concurrency)
        async def one(question):
            async with sem:
                verify()
                checkpoint=root/'answers'/f'{digest(question.id)}.json'
                if checkpoint.exists():
                    stored=json.loads(checkpoint.read_text())
                    if stored.get('identity')!=identity or digest(stored['result'])!=stored['digest']:
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
        result=RunResult(case.id,identity,runtime.bundle.version,answers,0 if graph is None else graph.graph.number_of_nodes(),
                         ({'status':'execution_error','error':graph_failure},) if graph_failure else graph.diagnostics)
        atomic_json(root/'result.json',result.to_dict())
        return result
