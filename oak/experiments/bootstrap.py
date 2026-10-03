"""Cold asset generation. Tasks declaring the fact-anchoring hook bootstrap F/C/P against a
fixed seed schema; every other task keeps the original protocol that also generates S."""
from __future__ import annotations

from oak.agents.protocol import ModelSession
from oak.kernel.assets import Asset, KernelAssets
from oak.kernel.validation import validate_bundle
from oak.runtime.artifacts import atomic_json, digest

ASSET_PROTOCOL='''Generate a task asset package, not framework code. Return JSON only, shaped {"assets":[...]}.
Every asset has id (stable safe identifier), kind F/C/P, content string, input_contract, output_contract,
schema_dependencies [] (filled by the framework), role (P only), stage (C only graph/answer), description,
trial_inputs: F MUST list at least one real, working parameter object; C and P use [].
Assign only to plain local variable names — never to dict items, list items or attributes
(no result["k"] = v, no obj.attr = v); build output dicts in one expression or with {...} literals.
Field rules: every C MUST set "stage" to "graph" or "answer"; every P MUST set "role" to one of
extract/tools/answer/review; F and C MUST NOT set role or stage.
The schema S is FIXED and supplied as fixed_schema; do not generate, extend or patch it.
Provide exactly one P for each of extract/tools/answer/review; at least one F and one C.
No H, paths, imports, permissions, pipeline, model calls or postprocessing.
The graph is fact-anchored: every atomic fact is an AtomicFact node and every other node traces back to fact
nodes. Node types: AtomicFact (id, text, predicate, polarity, modality); Entity (class from the schema's
declared entity classes, name); Value (dtype, canonical string value); Time (raw expression, precision,
optional start/end ISO, anchor_source_id); EvidenceSpan (source_id, verbatim quote, start_offset, end_offset);
Source (kind, document_id, location, speaker, date). Relations: subject, object_entity, object_value,
occurrence_time, evidence, locates, time_anchor.
Value fields hold canonical strings: convert with int()/float() before arithmetic; compare dates as ISO text.
F content is restricted Python syntax: one def run(params): with primitive local computation, if, for, comprehensions.
No imports, while, reflection, subscript/attribute assignment, global writes, nested functions or dynamic calls.
Only data capabilities: nodes(entity_type='',filters={},limit=100), search(terms,entity_type='',limit=40),
traverse(node_ids,relation,direction='out'), project(rows,fields), aggregate(rows,field='',operation='count/sum/min/max'),
order_by(rows,field,descending=False), date_difference(left_iso,right_iso).
Rows include node_id, entity_type, the node attributes above, source_ids. Fact rows expose text/predicate/
polarity/modality; entity rows expose class and name; time rows expose raw/precision/start/end/anchor_source_id.
Allowed builtins len,min,max,sum,sorted,set,dict,list,tuple,str,int,float,round,abs,enumerate,zip,range,bool,any,all,ceil,isinstance.
type() is not registered; use isinstance(x, str/int/float/bool) for type checks. The top-level snapshot is a frozen mapping: isinstance(snapshot, dict) is False; test keys with .get() or 'in', never with dict type checks.
Allowed methods get,keys,values,items,lower,upper,strip,split,splitlines,join,startswith,endswith,replace,isdigit,count,append,extend,add.
Inputs/operator values are immutable; append/extend only on newly created local lists. No mutation of dictionaries.
C content is one def check(candidate): returning exactly {"ok":bool,"issues":[nonempty strings]}, ok equals not issues.
C has no data capabilities. graph snapshot: stage,nodes. answer snapshot: stage,question,parameters,status,answer,
node_ids,evidence,visible_evidence,structured_answer (parsed JSON or null). Checks supplement mandatory guards.
F only gets declared params; it never sees question id or gold. Do not embed answers, complete-question matching or subject-specific query constants.
Contracts use type object/array/string/integer/number/boolean/null/any, properties,required,items,enum,additionalProperties,description only.
input_contract must describe F params. For C/P use {"type":"any"}. F output data or candidates, never control instructions.
P is behavioral text only; fixed framework owns output protocols. Optional template slots use ${schema} for all roles and ${tools} for tools.
Write adaptable task reasoning instructions; do not hardcode training names, question ids, answers or pipeline changes.
All assets must work when names, dates and request constraints change. Use parameterized retrieval functions and checks.
'''

LEGACY_ASSET_PROTOCOL='''Generate a task asset package, not framework code. Return JSON only, shaped {"assets":[...]}.
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
'''


class AssetBootstrapper:
    async def initialize(self, cases, spec, client, config, target, seed_schema=None):
        if isinstance(cases, tuple) and len(cases) == 1:
            cases = cases[0]
        if not isinstance(cases, (list, tuple)):
            cases = (cases,)
        corpus_blocks = [b for case in cases for b in case.corpus]
        questions = [q for case in cases for q in case.questions]
        seed_schema = seed_schema if seed_schema is not None else spec.seed_s
        anchored = bool(seed_schema and seed_schema.strip())
        session = ModelSession(client, config, 'bootstrap', limit=6)
        # Deterministic raw-data sampling, without labels, categories, graph caches or historical assets.
        blocks = []; size = 0
        for b in corpus_blocks:
            if size + len(b.text) > 24000: break
            blocks.append(b.to_dict()); size += len(b.text)
        payload = {'task': spec.declaration(),
                   'training_corpus': blocks,
                   'training_questions': [q.text for q in questions],
                   'corpus_fingerprint': digest([b.to_dict() for b in corpus_blocks])}
        if anchored:
            payload['fixed_schema'] = seed_schema

        def valid(obj):
            if set(obj) != {'assets'} or not isinstance(obj['assets'], list):
                raise ValueError('Expected an asset package, with no paths or permissions')
            items = []
            if anchored:
                seed = Asset('schema', 'S', seed_schema, {"type": "any"}, {"type": "any"}, (),
                             description='任务声明的固定种子 S（事实锚定）')
                generated = []
                for item in obj['assets']:
                    asset = Asset(**{**item, 'schema_dependencies': ['schema']})
                    if asset.kind == 'S':
                        if asset.content.strip() != seed_schema.strip():
                            raise ValueError('The schema is fixed; a modified S cannot be bootstrapped')
                        continue  # the seed echoed back unchanged is ignored
                    generated.append(asset)
                items = [seed.to_dict()] + [a.to_dict() for a in generated]
                origin = {'kind': 'cold_bootstrap', 'input_fingerprint': digest(payload), 'seed_assets': ['schema']}
            else:
                for item in obj['assets']:
                    normalized = dict(item)
                    if normalized.get('kind') == 'S':
                        normalized.setdefault('schema_dependencies', [])
                    items.append(normalized)
                origin = {'kind': 'cold_bootstrap', 'input_fingerprint': digest(payload), 'seed_assets': []}
            assets = KernelAssets.from_payload({'assets': items}, origin)
            # Static admission inside model feedback, before a version exists.
            from oak.schema.model import Schema
            from oak.operators.sandbox import admit
            schema = Schema.from_yaml(next(a.content for a in assets.assets if a.kind == 'S'))
            if schema.validate(): raise ValueError(str(schema.validate()))
            for a in assets.assets:
                if a.kind in {'F', 'C'}: admit(a.content, a.kind, [q.text for q in questions])
            return assets
        protocol = ASSET_PROTOCOL if anchored else LEGACY_ASSET_PROTOCOL
        try:
            assets = await session.request(config.bootstrap_role, protocol, payload, valid, max_tokens=14000)
        finally:
            atomic_json(target.parent / 'bootstrap-call.json', {'input_fingerprint': digest(payload),
                         'input': payload, 'raw_outputs': session.raw, 'events': session.events})
        bundle = assets.export(target)
        validate_bundle(bundle, [q.text for q in questions])
        return bundle
