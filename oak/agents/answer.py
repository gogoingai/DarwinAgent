"""Fixed collect -> generate -> check -> review -> retry -> publish flow."""
from __future__ import annotations

from oak.contracts import AnswerResult, plain
from oak.kernel.functions import DataCapabilities
from oak.kernel.validation import validate_candidate, validate_published
from .protocol import (ANSWER_PROTOCOL, TOOLS_PROTOCOL, REVIEW_PROTOCOL,
                       ModelSession, ProtocolError, validate_review)


class AnswerAgent:
    def __init__(self,runtime,client,config,spec,namespace):
        self.runtime,self.client,self.config,self.spec,self.namespace=runtime,client,config,spec,namespace

    async def answer(self,question,graph):
        from oak.runtime.artifacts import digest
        session=ModelSession(self.client,self.config,f'{self.namespace}_q_{digest(question.id)[:12]}',self.config.calls_per_question)
        caps=DataCapabilities(graph)
        visible=set();tool_results=[];feedback=[];trace=[]
        try:
            for attempt in range(self.config.answer_attempts):
                for step in range(self.config.tool_steps):
                    def valid_tool(obj):
                        if obj=={'action':'ready'}: return obj
                        if set(obj)!={'action','asset_id','parameters'} or obj['action']!='call' or obj['asset_id'] not in self.runtime.functions.functions:
                            raise ValueError('Unregistered tool or invalid control action')
                        return obj
                    action=await session.request(self.config.tools_role,
                        TOOLS_PROTOCOL+'\n任务工具指引：\n'+self.runtime.prompt('tools'),
                        {'question':question.text,'parameters':plain(question.parameters),
                         'tools':self.runtime.functions.descriptions(),'previous_results':tool_results,
                         'feedback':feedback},valid_tool)
                    if action['action']=='ready': break
                    result=self.runtime.call(action['asset_id'],action['parameters'],graph)
                    visible.update(result['node_ids']);tool_results.append(result)
                    trace.append({'stage':'tool','attempt':attempt,'step':step,**result})
                candidate=await session.request(self.config.answer_role,
                    ANSWER_PROTOCOL+'\n任务作答指引：\n'+self.runtime.prompt('answer'),
                    {'question':question.text,'parameters':plain(question.parameters),'answer_format':self.spec.answer_format,
                     'answer_contract':plain(self.spec.answer_contract),'tool_results':tool_results,
                     'visible_evidence':[caps.rows[x] for x in sorted(visible)],'feedback':feedback},
                    lambda obj: self._candidate(obj,visible))
                if candidate['status']=='abstained' and not any(t['read_operations'] for t in tool_results):
                    raise ProtocolError('Semantic abstention requires an actual data query',session.raw)
                snapshot,opinions=self.runtime.check_candidate(question,candidate,graph,visible)
                trace.append({'stage':'candidate','attempt':attempt,'candidate':candidate,'checks':opinions})
                failures=[x for x in opinions if not x['ok']]
                if failures:
                    feedback.append({'candidate':candidate,'task_checks':failures})
                    continue
                relevant=set(candidate['node_ids']) if candidate['status']=='answered' else visible
                source_ids={s for nid in relevant for s in caps.rows[nid]['source_ids']}
                # A refusal is audited against every graph node, not only the subset retrieved by F.
                # This is fixed review behavior; it never amends an answer or changes tool permissions.
                review_inputs=[]
                if candidate['status']=='answered':
                    review_inputs=[{'candidate':snapshot,'sources':[graph.sources[s].to_dict() for s in sorted(source_ids)]}]
                else:
                    import json
                    chunks=[];batch=[];size=0
                    for row in caps.rows.values():
                        row_size=len(json.dumps(row,ensure_ascii=False).encode())
                        if batch and size+row_size>self.config.result_bytes//2:
                            chunks.append(batch);batch=[];size=0
                        batch.append(row);size+=row_size
                    if batch: chunks.append(batch)
                    for index,rows in enumerate(chunks):
                        ids={s for row in rows for s in row['source_ids']}
                        review_inputs.append({'candidate':{**snapshot,'visible_evidence':rows},
                            'refusal_audit':{'chunk':index,'chunks':len(chunks),'covers_full_graph':True},
                            'sources':[graph.sources[s].to_dict() for s in sorted(ids)]})
                rejected=None
                for review_input in review_inputs:
                    review=await session.request(self.config.review_role,
                        REVIEW_PROTOCOL+'\n任务语义审查指引：\n'+self.runtime.prompt('review'),
                        review_input,lambda obj:validate_review(obj,candidate['status']))
                    trace.append({'stage':'review','attempt':attempt,**review})
                    if not review['accepted']:
                        rejected=review;break
                if rejected:
                    feedback.append({'candidate':candidate,'review':rejected})
                    continue
                evidence=tuple(graph.sources[s].source for s in sorted(source_ids)) if candidate['status']=='answered' else ()
                result=AnswerResult(question.id,candidate['status'],candidate['answer'],evidence,
                    node_ids=tuple(candidate['node_ids']),raw_outputs=tuple(session.raw),trace=tuple(trace))
                validate_published(result,question,graph)
                return result
            raise ProtocolError('Feedback retries exhausted without a publishable candidate',session.raw)
        except Exception as exc:
            trace.append({'stage':'execution_error','type':type(exc).__name__,'error':str(exc),'events':session.events})
            return AnswerResult(question.id,'execution_error','',error=f'{type(exc).__name__}: {exc}',
                                raw_outputs=tuple(session.raw),trace=tuple(trace))

    def _candidate(self,obj,visible):
        validate_candidate(obj,visible,self.spec)
        return obj
