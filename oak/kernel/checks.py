"""Task C returns opinions. Fixed validation and publication do not live here."""
from __future__ import annotations

from oak.operators.sandbox import Interpreter, Limits, admit


class CheckRegistry:
    def __init__(self,bundle,limits=Limits(),forbidden_questions=()):
        self.bundle,self.limits=bundle,limits
        self.checks={a.id:(a,admit(a.content,'C',forbidden_questions))
                     for a in bundle.assets.assets if a.kind=='C'}

    def budget(self,snapshot):
        """检查步数预算随受检快照规模伸缩（图阶段 C 必须能遍历全图），但保持受限上限。
        agentic_v3 首轮事故：1112 节点的图检查需 41,706 步 > function_steps=30,000。"""
        nodes=snapshot.get('nodes') if isinstance(snapshot,dict) else None
        scale=80*len(nodes) if isinstance(nodes,(list,tuple)) else 0
        steps=min(self.limits.steps*20,self.limits.steps+scale)
        from dataclasses import replace
        return replace(self.limits,steps=steps)

    def run(self,stage,snapshot):
        self.bundle.verify()
        opinions=[]
        limits=self.budget(snapshot)
        for a,fn in self.checks.values():
            if a.stage!=stage: continue
            import time as _t; started=_t.monotonic()
            interp=Interpreter(fn,{},limits)
            result=interp.execute(snapshot)
            if not isinstance(result,dict) or set(result)!={'ok','issues'} or type(result['ok']) is not bool or not isinstance(result['issues'],list) or not all(isinstance(x,str) and x.strip() for x in result['issues']):
                raise ValueError('C must return {ok: bool, issues: [nonempty string]}')
            if result['ok'] != (not result['issues']): raise ValueError('Inconsistent C opinion')
            opinions.append({'check_id':a.id,'fingerprint':a.fingerprint,
                             'steps_used':interp.steps,'step_budget':limits.steps,
                             'elapsed_ms':round(1000*(_t.monotonic()-started),1),**result})
        return opinions


def enforce_opinions(opinions, context=''):
    """图阶段 C 的否决必须被采纳（评审二）：任一 ok=False 即拒绝准入，check_id、
    issues 与预算信息随错误反馈给资产生成模型修订。冷启动、候选预检、外测同一条规则。"""
    failures = [o for o in opinions if not o.get('ok')]
    if failures:
        detail = [{'check_id': o.get('check_id'), 'issues': o.get('issues'),
                   'steps_used': o.get('steps_used'), 'step_budget': o.get('step_budget')}
                  for o in failures]
        raise ValueError(f'{context}图检查否决: {detail}')
    return opinions


def synthetic_answer_snapshot(rows, question_text, parameters=None):
    """良好成形的答案阶段检查快照（真实记忆行＋真实问题文本）。冷启动准入用它真实执行
    答案阶段 C（agentic_v6 G1 B0 全灭事故：模型自写 C 结构不兼容、全盘否决每个候选，
    而答案阶段 C 此前只有静态 admit、从未被执行过）。"""
    import json as _json
    evidence = list(rows[:1])
    answer = str(evidence[0].get('陈述') or evidence[0].get('statement') or '记忆支持的陈述')
    try:
        structured = _json.loads(answer)
    except Exception:
        structured = None
    return {'stage': 'answer', 'question': question_text, 'parameters': parameters or {},
            'status': 'answered', 'answer': answer,
            'node_ids': [r['node_id'] for r in evidence],
            'evidence': evidence, 'visible_evidence': list(rows[:3]),
            'structured_answer': structured}
