"""Fixed collect -> generate -> check -> review -> retry -> publish flow."""
from __future__ import annotations

import asyncio

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
        vector_once=self.config.retrieval_mode=='vector_once'
        try:
            for attempt in range(self.config.answer_attempts):
                if vector_once:
                    # V0 纯向量基线：一次确定性检索即全部证据预算——无工具循环，检索后冻结；
                    # 作答/检查/审查流程与 G1 完全共享。
                    rows=await asyncio.to_thread(caps.semantic_search,question.text,
                                                 limit=self.config.vector_k)
                    visible={r['node_id'] for r in rows}
                    tool_results=[{'asset_id':'vector_once','asset_fingerprint':'',
                                   'data':rows,'node_ids':sorted(visible),
                                   'source_ids':sorted({s for r in rows for s in r['source_ids']}),
                                   'read_operations':caps.read_operations}]
                    trace.append({'stage':'retrieval','mode':'vector_once','k':self.config.vector_k,
                                  'rows':len(rows)})
                else:
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
                        aid=action['asset_id']
                        asset=self.runtime.functions.functions[aid][0]
                        from oak.runtime.artifacts import digest
                        call={'stage':'call_start','attempt':attempt,'step':step,
                              'asset_id':aid,'asset_fingerprint':asset.fingerprint,
                              'input_ref':digest(plain(action['parameters'])),
                              'parameters':plain(action['parameters'])}
                        trace.append(call)
                        observation={}
                        try:
                            result=self.runtime.call(aid,action['parameters'],graph,observation)
                        except ValueError as exc:
                            if not str(exc).startswith('tool.params:'):
                                raise
                            # 参数契约违规进反馈环（2026-10-07 运行二事故：DeepSeek 工具
                            # 调用发明未声明字段 {'主题'}，一击致命成题级确定性故障，B0 门
                            # 即挂）。调用仍被拒（契约不放松），但同题内给出「未声明字段
                            # ＋合法 schema」反馈让模型重试；步数与协议重试天然有界，
                            # 反复不改才由既有 ProtocolError 兜底成故障。
                            contract=asset.input_contract or {}
                            allowed=sorted((contract.get('properties') or {}).keys())
                            trace.append({'stage':'tool_error',**{k:v for k,v in call.items()
                                          if k!='stage'},'error_type':'ValueError',
                                          'error':str(exc),'observation':observation})
                            feedback.append({'tool_call_reject':{'asset_id':aid,
                                'parameters':plain(action['parameters']),'error':str(exc),
                                'allowed_fields':allowed,
                                'hint':'工具参数违反声明契约：未声明字段会被拒绝执行；'
                                      '请只用 allowed_fields 里的字段重新调用'}})
                            continue
                        except Exception as exc:
                            trace.append({'stage':'tool_error',**{k:v for k,v in call.items()
                                          if k!='stage'},'error_type':type(exc).__name__,
                                          'error':str(exc),'observation':observation})
                            raise
                        visible.update(result['node_ids']);tool_results.append(result)
                        # 参数在真实执行点入轨迹（评审②）：协议重试中被拒的旧动作不会错配到
                        # 成功调用上；反馈摘要据此读取，不再依赖 raw_outputs 顺序配对。
                        trace.append({'stage':'tool','attempt':attempt,'step':step,
                                      'parameters':plain(action['parameters']),
                                      'observation':observation,**result})
                candidate=await session.request(self.config.answer_role,
                    ANSWER_PROTOCOL+'\n任务作答指引：\n'+self.runtime.prompt('answer'),
                    {'question':question.text,'parameters':plain(question.parameters),'answer_format':self.spec.answer_format,
                     'answer_contract':plain(self.spec.answer_contract),'tool_results':tool_results,
                     'visible_evidence':[caps.rows[x] for x in sorted(visible)],'feedback':feedback},
                    lambda obj: self._candidate(obj,visible))
                if candidate['status']=='abstained' and not any(t['read_operations'] for t in tool_results):
                    # 拒答前必须做过实际查询：作为反馈给重试机会（与候选校验同路），
                    # 重试耗尽仍无查询才整体失败。
                    feedback.append({'candidate':candidate,
                                     'task_checks':[{'check_id':'fixed.query_before_abstain','ok':False,
                                                     'issues':['语义拒答前必须先完成至少一次实际数据查询']}]})
                    continue
                snapshot,opinions=self.runtime.check_candidate(question,candidate,graph,visible)
                trace.append({'stage':'candidate','attempt':attempt,'candidate':candidate,'checks':opinions})
                failures=[x for x in opinions if not x['ok']]
                if failures:
                    # C 失败候选的检查快照原地留存（可验证经验回放原料，2026-10-05 Travel
                    # 冻结容器误判事故）：输入与判定事实原样进检查点，供后续候选准入回放；
                    # visible_evidence 行数封顶防爆轨迹，整体超限则只留截断标记。
                    saved=plain(snapshot)
                    if len(saved.get('visible_evidence') or ())>60:
                        saved['visible_evidence']=list(saved['visible_evidence'][:60])
                        saved['visible_evidence_truncated']=True
                    import json as _json
                    if len(_json.dumps(saved,ensure_ascii=False,default=str))<=200000:
                        trace[-1]['check_snapshot']=saved
                    else:
                        trace[-1]['check_snapshot_truncated']=True
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
                    # 拒答审计证据范围两臂统一（评审#5）：只看已召回证据及其来源，不读全图——
                    # 融合版需要补证必须显式调用登记工具（调用/返回/成本都进轨迹），
                    # 不得借审计通道隐式获得未召回的全量信息，否则召回收益归因被混淆。
                    rows=[caps.rows[x] for x in sorted(visible)]
                    ids={s for row in rows for s in row['source_ids']}
                    review_inputs=[{'candidate':{**snapshot,'visible_evidence':rows},
                                    'refusal_audit':{'covers_full_graph':False,
                                                     'mode':'vector_once' if vector_once else 'agentic_retrieved'},
                                    'sources':[graph.sources[s].to_dict() for s in sorted(ids)]}]
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
                # 发布点契约错误进反馈环（缺口⑤⑥修复，2026-10-06）：此前 AnswerResult
                # 构造/validate_published 抛 ValueError 会直接落到兜底 except 记执行
                # 故障、不给模型重答机会。现改为与检查/审查拒绝同路：反馈里给出
                # 逐引用节点的来源数（含无出处行——缺口⑥的显式处理），重试耗尽才
                # 由既有 ProtocolError 兜底。
                try:
                    result=AnswerResult(question.id,candidate['status'],candidate['answer'],evidence,
                        node_ids=tuple(candidate['node_ids']),raw_outputs=tuple(session.raw),trace=tuple(trace))
                    validate_published(result,question,graph)
                except ValueError as exc:
                    cited_sources={nid:(len(caps.rows[nid]['source_ids']) if nid in caps.rows else -1)
                                   for nid in candidate['node_ids'] or ()}
                    feedback.append({'candidate':candidate,'publish_reject':{
                        'error':str(exc),
                        'cited_node_source_counts':cited_sources,
                        'hint':'answered 候选必须引用带出处（source_ids 非空）的事实行；'
                              '来源数为 0 的行不可作为证据，请改引有出处的行或如实拒答'}})
                    continue
                return result
            raise ProtocolError('Feedback retries exhausted without a publishable candidate',session.raw)
        except Exception as exc:
            trace.append({'stage':'execution_error','type':type(exc).__name__,'error':str(exc),'events':session.events})
            return AnswerResult(question.id,'execution_error','',error=f'{type(exc).__name__}: {exc}',
                                raw_outputs=tuple(session.raw),trace=tuple(trace))

    def _candidate(self,obj,visible):
        validate_candidate(obj,visible,self.spec)
        return obj
