"""Task C returns opinions. Fixed validation and publication do not live here."""
from __future__ import annotations

from oak.operators.sandbox import Interpreter, Limits, admit


class CheckRegistry:
    def __init__(self,bundle,limits=Limits(),forbidden_questions=()):
        self.bundle,self.limits=bundle,limits
        self.checks={a.id:(a,admit(a.content,'C',forbidden_questions))
                     for a in bundle.assets.assets if a.kind=='C'}

    def run(self,stage,snapshot):
        self.bundle.verify()
        opinions=[]
        for a,fn in self.checks.values():
            if a.stage!=stage: continue
            result=Interpreter(fn,{},self.limits).execute(snapshot)
            if not isinstance(result,dict) or set(result)!={'ok','issues'} or type(result['ok']) is not bool or not isinstance(result['issues'],list) or not all(isinstance(x,str) and x.strip() for x in result['issues']):
                raise ValueError('C must return {ok: bool, issues: [nonempty string]}')
            if result['ok'] != (not result['issues']): raise ValueError('Inconsistent C opinion')
            opinions.append({'check_id':a.id,'fingerprint':a.fingerprint,**result})
        return opinions
