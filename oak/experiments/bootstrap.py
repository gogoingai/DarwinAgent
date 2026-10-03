"""Cold generation of task F/C/P against the task's fixed seed schema, without history."""
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
type() is not registered; use isinstance(x, str/int/float/bool) for type checks.
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


class AssetBootstrapper:
    async def initialize(self, case, spec, client, config, target, seed_schema=None):
        seed_schema = seed_schema if seed_schema is not None else spec.seed_s
        if not seed_schema or not seed_schema.strip():
            raise ValueError('Cold bootstrap requires the task seed schema (task.yaml seed.S)')
        session = ModelSession(client, config, 'bootstrap', limit=6)
        # Deterministic raw-data sampling, without labels, categories, graph caches or historical assets.
        blocks = []; size = 0
        for b in case.corpus:
            if size + len(b.text) > 24000: break
            blocks.append(b.to_dict()); size += len(b.text)
        payload = {'task': spec.declaration(), 'fixed_schema': seed_schema,
                   'training_corpus': blocks,
                   'training_questions': [q.text for q in case.questions],
                   'corpus_fingerprint': digest([b.to_dict() for b in case.corpus])}

        def valid(obj):
            if set(obj) != {'assets'} or not isinstance(obj['assets'], list):
                raise ValueError('Expected an asset package, with no paths or permissions')
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
            assets = KernelAssets.from_payload({'assets': [seed.to_dict()] + [a.to_dict() for a in generated]},
                {'kind': 'cold_bootstrap', 'input_fingerprint': digest(payload), 'seed_assets': ['schema']})
            # Static admission inside model feedback, before a version exists.
            from oak.schema.model import Schema
            from oak.operators.sandbox import admit
            schema = Schema.from_yaml(next(a.content for a in assets.assets if a.kind == 'S'))
            if schema.validate(): raise ValueError(str(schema.validate()))
            for a in assets.assets:
                if a.kind in {'F', 'C'}: admit(a.content, a.kind, [q.text for q in case.questions])
            return assets
        try:
            assets = await session.request(config.bootstrap_role, ASSET_PROTOCOL, payload, valid, max_tokens=14000)
        finally:
            atomic_json(target.parent / 'bootstrap-call.json', {'input_fingerprint': digest(payload),
                         'input': payload, 'raw_outputs': session.raw, 'events': session.events})
        bundle = assets.export(target)
        validate_bundle(bundle, [q.text for q in case.questions])
        return bundle
