"""Cold generation of all task S/F/C/P from declared training inputs, without seeds."""
from __future__ import annotations

from oak.agents.protocol import ModelSession
from oak.kernel.assets import KernelAssets
from oak.kernel.validation import validate_bundle
from oak.runtime.artifacts import atomic_json, digest

ASSET_PROTOCOL='''Generate a task asset package, not framework code. Return JSON only, shaped {"assets":[...]}.
Every asset has id (stable safe identifier), kind S/F/C/P, content string, input_contract, output_contract,
schema_dependencies [schema id] (S has []), role (P only), stage (C only graph/answer), description,
trial_inputs (F only, at least one actual parameter object, otherwise []). Exactly one S and one P for each extract/tools/answer/review;
at least one F and one C. No H, paths, imports, permissions, pipeline, model calls or postprocessing.
S content is YAML: entity_types map of types with primary_key list, attributes [{name,dtype}], description;
relation_types map with domain/range, axioms list. dtype: string/int/float/date/bool. Every key must be a declared attribute.
F content is restricted Python syntax: one def run(params): with primitive local computation, if, for, comprehensions.
No imports, while, reflection, subscript/attribute assignment, global writes, nested functions or dynamic calls.
Only data capabilities: nodes(entity_type='',filters={},limit=100), search(terms,entity_type='',limit=40),
traverse(node_ids,relation,direction='out'), project(rows,fields), aggregate(rows,field='',operation='count/sum/min/max'),
order_by(rows,field,descending=False), date_difference(left_iso,right_iso).
Rows include node_id, entity_type, schema attributes, source_ids, claims with source_id/quote/key/properties.
Allowed builtins len,min,max,sum,sorted,set,dict,list,tuple,str,int,float,round,abs,enumerate,zip,range,bool,any,all,ceil.
Allowed methods get,keys,values,items,lower,upper,strip,split,splitlines,join,startswith,endswith,replace,isdigit,count,append,extend,add.
Inputs/operator values are immutable; append/extend only on newly created local lists. No mutation of dictionaries.
C content is one def check(candidate): returning exactly {"ok":bool,"issues":[nonempty strings]}, ok equals not issues.
C has no data capabilities. graph snapshot: stage,nodes. answer snapshot: stage,question,parameters,status,answer,
node_ids,evidence,visible_evidence,structured_answer (parsed JSON or null). Checks supplement mandatory guards.
F only gets declared params; it never sees question id or gold. Do not embed answers, complete-question matching or subject-specific query constants.
Contracts use type object/array/string/integer/number/boolean/null/any, properties,required,items,enum,additionalProperties,description only.
input_contract must describe F params. For C/P/S use {"type":"any"}. F output data or candidates, never control instructions.
P is behavioral text only; fixed framework owns output protocols. Optional template slots use ${schema} for all roles and ${tools} for tools.
Write adaptable task reasoning instructions; do not hardcode training names, question ids, answers or pipeline changes.
All assets must work when names, dates and request constraints change. Use parameterized retrieval functions and checks.
'''.replace('\n+','\n')


class AssetBootstrapper:
    async def initialize(self,case,spec,client,config,target):
        session=ModelSession(client,config,'bootstrap',limit=6)
        # Deterministic raw-data sampling, without labels, categories, graph caches or historical assets.
        blocks=[];size=0
        for b in case.corpus:
            if size+len(b.text)>24000: break
            blocks.append(b.to_dict());size+=len(b.text)
        payload={'task':spec.declaration(),'training_corpus':blocks,
                 'training_questions':[q.text for q in case.questions],
                 'corpus_fingerprint':digest([b.to_dict() for b in case.corpus])}
        def valid(obj):
            assets=KernelAssets.from_payload(obj,{'kind':'cold_bootstrap','input_fingerprint':digest(payload),'seed_assets':[]})
            # Static admission inside model feedback, before a version exists.
            from oak.schema.model import Schema
            from oak.operators.sandbox import admit
            schema=Schema.from_yaml(next(a.content for a in assets.assets if a.kind=='S'))
            if schema.validate(): raise ValueError(str(schema.validate()))
            for a in assets.assets:
                if a.kind in {'F','C'}: admit(a.content,a.kind,[q.text for q in case.questions])
            return assets
        try:
            assets=await session.request(config.bootstrap_role,ASSET_PROTOCOL,payload,valid,max_tokens=14000)
        finally:
            atomic_json(target.parent/'bootstrap-call.json',{'input_fingerprint':digest(payload),
                         'input':payload,'raw_outputs':session.raw,'events':session.events})
        bundle=assets.export(target)
        validate_bundle(bundle,[q.text for q in case.questions])
        return bundle
