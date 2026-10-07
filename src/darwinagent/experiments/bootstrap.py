"""Cold asset generation. Tasks declaring the fact-anchoring hook bootstrap F/C/P against a
fixed seed schema; every other task keeps the original protocol that also generates S.

Protocols are composed from three parts: a phase header (output format, S policy, package
shape), a data segment describing the graph vocabulary, and the shared F/C/P core rules. The
revision protocol in revision_protocol() derives from the SAME segments as the bundle's real
graph mode, so a legacy bundle never sees fact-anchoring instructions and every prompt
carries exactly one output format."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

from darwinagent.agents.protocol import ModelSession
from darwinagent.contracts import plain
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.validation import capability_names, validate_bundle
from darwinagent.runtime.artifacts import atomic_json, digest


def _trial_failure_feedback(report):
    failures = [row for row in report.get('scenarios', ())
                if row.get('required') and row.get('status') != 'passed']
    grouped = Counter()
    scenarios = {}
    for row in failures:
        key = (row.get('asset_id'), row.get('status'),
               str(row.get('error', row.get('status')))[:450])
        grouped[key] += 1
        scenarios.setdefault(key, set()).add(str(row.get('scenario_id', 'unknown')))
    details = [
        f'{asset} [{status}, {count} 次; 场景 {", ".join(sorted(scenarios[(asset, status, error)]))}]: {error}'
        for (asset, status, error), count in grouped.items()]
    traversal_hint = (
        '高出入度样例依据 traverse 的真实参数绑定生成；内部找种子的 F 也会在图行序压力副本上执行。'
        '请核对失败参数、实际遍历方向与匹配边；不要为了通过检查增加未使用的参数。'
        if any(row.get('scenario_id', '').startswith('high_degree_') for row in failures)
        else '')
    representatives={}
    for row in failures:
        representatives.setdefault((row.get('asset_id'),row.get('scenario_id'),row.get('error_type')),row)
    observations=[]
    for row in list(representatives.values())[:6]:
        detail={k:row.get(k) for k in ('asset_id','scenario_id','parameters','steps_used',
                                      'step_budget','traverse_observations') if row.get(k) is not None}
        import json
        observations.append(json.dumps(detail,ensure_ascii=False)[:650])
    resource_hint=('步骤超限时，先限制进入逐行投影/去重循环的候选数；不要在昂贵处理结束后才截断。'
                   '保留真实过滤和证据，报告未处理量；不提高预算。'
                   if any(row.get('error_type')=='SandboxError' for row in failures) else '')
    return (f'真实图试跑拒绝：{len(failures)} 个必需场景失败、{len(grouped)} 类独立错误。'
            '请按以下全部独立错误一次修正，勿逐字段修补同一数据行的 output_contract；'
            '对可变行字段使用 additionalProperties true，同时保留返回证据、必需工具能力和合法试跑。'
            + traversal_hint + resource_hint + ' | '.join(details)
            + (' 实际执行观察：'+' | '.join(observations) if observations else ''))

_CORE_RULES='''Assign to plain local variable names or to items of dicts/lists you created locally
(out["k"] = v on your own dict is fine; inputs are immutable and reject writes);
never assign to attributes (no obj.attr = v).
Field rules: every C MUST set "stage" to "graph" or "answer"; every P MUST set "role" to one of
extract/tools/answer/review; F MUST NOT set role or stage. C MUST NOT set role; P MUST NOT set stage.
No H, paths, imports, permissions, pipeline, model calls or postprocessing.
F content is restricted Python syntax: one def run(params): with primitive local computation, if, for, comprehensions.
No imports, while, reflection, subscript/attribute assignment, global writes, nested functions or dynamic calls.
Only data capabilities: nodes(entity_type='',filters={},limit=100), search(terms,entity_type='',limit=40),
traverse(node_ids,relation,direction='out'), project(rows,fields), aggregate(rows,field='',operation='count/sum/min/max'),
order_by(rows,field='',descending=False), date_difference(left_iso,right_iso),
semantic_search(query,subject='',limit=8) when the graph carries a frozen vector index,
relative_date(anchor_iso,expression) resolving Chinese relative dates against an ISO anchor.
Operator bounds: nodes limit 1..5000; search limit 1..200, at most 30 terms each 1..100 characters.
traverse accepts a node_id string or a sequence of AT MOST 100 public node_ids; an empty sequence is legal and returns no rows.
Bound node_ids before traverse; use only IDs actually returned by data capabilities. Do not confuse input-seed bounds with output-row bounds.
Rows include node_id, entity_type, node attributes, source_ids. Allowed builtins
len,min,max,sum,sorted,set,dict,list,tuple,str,int,float,round,abs,enumerate,zip,range,bool,any,all,ceil,isinstance.
type() is not registered; use isinstance(x, str/int/float/bool) for type checks. The top-level snapshot is a frozen mapping: isinstance(snapshot, dict) is False; test keys with .get() or 'in', never with dict type checks.
Allowed methods get,keys,values,items,lower,upper,strip,split,splitlines,join,startswith,endswith,replace,isdigit,count,append,extend,add.
Inputs/operator values are immutable; append/extend only on newly created local lists. No mutation of dictionaries.
The interpreter recursively freezes input JSON arrays as tuples and objects as read-only mappings.
isinstance(input_array,list) and isinstance(input_object,dict) are therefore FALSE, even for valid JSON.
Use isinstance(array,(list,tuple)) for sequences. Access input mappings with get/items/subscripts;
dict(row) creates a local mapping if a concrete dict is needed. Do not confuse immutability with bad data.
C content is one def check(candidate): returning exactly {"ok":bool,"issues":[nonempty strings]}, ok equals not issues.
C has no data capabilities. graph snapshot: stage,nodes. answer snapshot: stage,question,parameters,status,answer,
node_ids,evidence,visible_evidence,structured_answer (parsed JSON or null). Checks supplement mandatory guards.
F only gets declared params; it never sees question id or gold. Do not embed answers, complete-question matching or subject-specific query constants.
Contracts use type object/array/string/integer/number/boolean/null/any, properties,required,items,enum,additionalProperties,description only.
The type MUST be a single string; JSON-Schema unions like ["string","null"] are unsupported.
P.answer for json tasks: the serialized answer must satisfy the task answer_contract exactly;
values with no data normalize to the declared type (string -> empty string, object -> {}, array -> []),
never null and never omitted for required fields (2026-10-05 Travel loop2 冒烟拦截：answer[*].lunch null)。
Normalize projected nullable fields to the declared scalar type, or use type any for intentionally variable fields.
project(rows,fields) returns a list of row objects, never a column-value list.
For large traverse results, bound candidates BEFORE per-row projection/deduplication and report omitted rows; slicing after an unbounded loop does not bound steps.
input_contract must describe F params. For C/P (and S where the package includes one) use {"type":"any"}. F output data or candidates, never control instructions.
Evidence surface: the rows an F RETURNS are the answerable evidence; rows read internally but not
returned stay provenance-only (read lineage) and never reach answering. An aggregating or computing
F must include the supporting rows (or their node_id list) in its return so computed answers stay citable.
Filter completeness (hard requirement): exact-match conditions (subject, type, theme equality) MUST go
into nodes(filters=...) so matching happens over the WHOLE graph before any limit. Only inexact
matching (substring, date-prefix, semantic) may post-process a fetched candidate set - and then the F
must scan with an explicit scan budget separate from its return limit (e.g. fetch in pages or use a
high scan limit) and include in its result a truncation note (how many rows were scanned, whether
more remain). Never present a truncated row set as the complete set.
Graph recall expansion: an F may chain search/nodes -> traverse(related entity or topic) -> back to
atomic facts in ONE function (fact -> entity -> related facts), returning facts with their sources
and the relation path, deduplicated. This retrieves facts keyword/vector search miss. Use it when the
evidence gap is structural (missing set members, related-entity facts), not on every question.
String conversion applies to SCALARS only: calling str() on a list/dict/tuple raises
"Container-to-string conversion is unsupported" and kills the whole question. To build text
from containers, loop their scalar elements and join. This rule applies to EVERY function you
write, not only the one that failed before.
F execution budgets (hard): at most 30000 interpreter steps, 15 seconds, and a 180KB result -
"budget exhausted" means your F scans or loops too much: narrow it with nodes(filters=...) or
search terms instead of scanning without a limit. No try/except and no while (restricted Python).
If F revisions keep failing admission across rounds, submit P first (P never executes in
the sandbox) and retry the F idea later.
Frozen inputs arrive as tuples: never isinstance(...,list)-guard or reset them to []; iterate directly
or rebuild with list(...). Output contracts must tolerate missing attributes: .get() yields None for
absent fields, so declare nullable fields as ["string","null"] or omit them - a plain string-typed
field rejects the whole result when a row lacks it.
Tool-loop discipline for P.tools: choose tools by the CURRENT evidence gap - subject unclear (confirm
name/alias, or drop the subject filter), missing set members (complementary query, structural filter,
or relation expansion), time conflict (fetch the same event's sources and session-date anchor),
empty or duplicate results (change the constraint, tool, or exploration direction - never re-issue
the same query with reworded parameters). Decide stop vs continue on evidence sufficiency, not on a
fixed retrieval order.
Rows produced by one tool often flow into another tool's params: when a parameter takes rows (or row lists),
declare its item objects with additionalProperties true — tool outputs carry runtime fields (node_id,
entity_type, source_ids, score) beyond the task attributes.
F output contracts must reuse the memory structure sample's real field names and shapes exactly
(e.g. rows carry source_ids as a list; dates are ISO strings or empty); do not invent variants.
Capability results are immutable sequences (tuples): combine with list(a)+list(b) or [*a,*b], never
mutate them; build fresh lists with comprehensions.
P is behavioral text only; fixed framework owns output protocols. Optional template slots use ${schema} for all roles and ${tools} for tools.
Write adaptable task reasoning instructions; do not hardcode training names, question ids, answers or pipeline changes.
All assets must work when names, dates and request constraints change. Use parameterized retrieval functions and checks.
'''

_ANCHORED_DATA='''The graph is fact-anchored: every atomic fact is an AtomicFact node and every other node traces back to fact
nodes. Node types: AtomicFact (id, text, predicate, polarity, modality); Entity (class from the schema's
declared entity classes, name); Value (dtype, canonical string value); Time (raw expression, precision,
optional start/end ISO, anchor_source_id); EvidenceSpan (source_id, verbatim quote, start_offset, end_offset);
Source (kind, document_id, location, speaker, date). Relations: subject, object_entity, object_value,
occurrence_time, evidence, locates, time_anchor.
Value fields hold canonical strings: convert with int()/float() before arithmetic; compare dates as ISO text.
Fact rows expose text/predicate/polarity/modality; entity rows expose class and name; time rows expose
raw/precision/start/end/anchor_source_id.
'''

_LEGACY_DATA='''S content is YAML: entity_types map of types with primary_key list, attributes [{name,dtype}], description;
relation_types map with domain/range, axioms list. dtype: string/int/float/date/bool. Every key must be a declared attribute.
Rows include claims with source_id/quote/key/properties.
'''

_BOOTSTRAP_ASSETS_OUTPUT='Generate a task asset package, not framework code. Return JSON only, shaped {"assets":[...]}.\n'

_BOOTSTRAP_ANCHORED_HEADER=_BOOTSTRAP_ASSETS_OUTPUT+'''Every asset has id (stable safe identifier), kind F/C/P, content string, input_contract, output_contract,
schema_dependencies [] (filled by the framework), role (P only), stage (C only graph/answer), description,
trial_inputs: F MUST list at least one real, working parameter object; C and P use [].
The schema S is FIXED and supplied as fixed_schema; do not generate, extend or patch it.
Provide exactly one P for each of extract/tools/answer/review; at least one F. C (task checks) is optional.
'''

_BOOTSTRAP_LEGACY_HEADER=_BOOTSTRAP_ASSETS_OUTPUT+'''Every asset has id (stable safe identifier), kind S/F/C/P, content string, input_contract, output_contract,
schema_dependencies [schema id] (S has []), role (P only), stage (C only graph/answer), description,
trial_inputs (F only, at least one actual parameter object, otherwise []). Exactly one S and one P for each of extract/tools/answer/review;
at least one F. C (task checks) is optional.
'''

_ATOMIC_MEMORY_CLAUSE='''Hard minimum for this task's schema S: declare at least one atomic-memory node type and name it in
meta.atomic_memory_type; that type MUST carry at least the attributes 编号 (memory id) and 陈述 (statement).
Everything else about S is yours to design from the memory structure sample: additional attributes, other node
types, relation types and axioms as the task needs. The declared atomic-memory type is frozen after admission.
The package MUST include at least one retrieval F that actually queries the graph (nodes/search/traverse/
semantic_search) so answering starts from retrieved memory, not prior knowledge.
The frozen memory graph already exists: its node type names are the exact keys of memory_structure.node_types
and its relation names the keys of memory_structure.relations. S MUST declare those same names verbatim
(including meta.atomic_memory_type being one of them); inventing synonyms makes every query miss.
'''

_C_DISCIPLINE_CLAUSE='''C discipline (hard requirement): a C asset performs MECHANICAL structural checks only
(required fields present, citation format consistent, status/shape valid). Semantic quality
and factual judgment belong EXCLUSIVELY to the review stage. A C that rejects a well-formed
answer because of style, brevity, wording or its own quality opinions will be rejected at
admission - it runs against single-fact, list-style and valid-abstention samples. Include a
C only if it accepts all three; omitting C entirely is always acceptable.
'''

_RETRIEVAL_FLOOR_CLAUSE='''Retrieval-tool floor for this task (hard admission requirement): the package MUST register
  - at least one semantic vector-retrieval F that calls semantic_search(...) internally (similarity
    search over the frozen memory vector index), AND
  - at least one relation F that calls traverse(...) internally (graph expansion along relations).
Keyword search (nodes/search) alone does NOT satisfy either requirement. Both F must pass trials on the
real memory snapshot; the floor is frozen after admission — revisions may not remove the last F of a class.
'''

_REVISION_OUTPUT='''This is one revision, not a fresh bootstrap. Return JSON only, shaped {"patches":[{"asset":a complete asset
object,"base_fingerprint":current asset fingerprint (null for a new asset),"reason":"diagnosis",
"training_evidence":[training_id values taken from the questions list]}]}. Patches are the only accepted
output format. No paths, commands or framework changes.
'''

_REVISION_ANCHORED_HEADER=_REVISION_OUTPUT+'''S patches may only EXTEND the seed schema: additional entity classes in meta.entity_classes, additional
axioms or node/relation types. The meta.anchoring declaration and the existing anchoring vocabulary are
frozen — admission rejects drops or alterations.
'''

_REVISION_LEGACY_HEADER=_REVISION_OUTPUT+'''S may be revised as the YAML entity/relation schema, but every change must keep the existing F and C assets
working: admission re-runs their trials against the revised schema before a candidate exists.
'''

_REVISION_TAIL='''Address generalizable causes in task assets. Scoring references are diagnostic only, never hardcoded generation answers.
'''

ASSET_PROTOCOL=_BOOTSTRAP_ANCHORED_HEADER+_ANCHORED_DATA+_CORE_RULES
LEGACY_ASSET_PROTOCOL=_BOOTSTRAP_LEGACY_HEADER+_LEGACY_DATA+_CORE_RULES


def revision_protocol(base, allowed_kinds=()):
    """Revision protocol consistent with the bundle's real graph mode, assembled from the
    same segments as its bootstrap protocol: legacy bundles keep the entity/relation
    vocabulary and a revisable S; anchored bundles keep fact-anchoring with an extend-only
    seed. Either way patches are the single output format. The allowed-asset scope and the
    verbatim fingerprint rule are stated explicitly so admission rejections stay rare."""
    from darwinagent.schema.model import Schema
    schema_yaml = next(a.content for a in base.assets.assets if a.kind == 'S')
    anchored = bool(Schema.from_yaml(schema_yaml).meta.get('anchoring'))
    scope_line = '\n'
    if allowed_kinds:
        scope_line += (f'This round may only patch asset kinds {sorted(set(allowed_kinds))}; patches on other '
                       'kinds are rejected at admission.\n')
    scope_line += ('Every patch base_fingerprint must equal the fingerprint field of the matching asset in the '
                   'payload, copied verbatim; a mistyped or stale fingerprint is rejected.\n')
    if anchored:
        return _REVISION_ANCHORED_HEADER + _ANCHORED_DATA + _CORE_RULES + _REVISION_TAIL + scope_line
    return _REVISION_LEGACY_HEADER + _LEGACY_DATA + _CORE_RULES + _REVISION_TAIL + scope_line



class AssetBootstrapper:
    async def initialize(self, cases, spec, client, config, target, seed_schema=None,
                         structure_sample=None, trial_graph=None, snapshot_root=None,
                         trial_record=None, trial_supply=None):
        if isinstance(cases, tuple) and len(cases) == 1:
            cases = cases[0]
        if not isinstance(cases, (list, tuple)):
            cases = (cases,)
        corpus_blocks = [b for case in cases for b in case.corpus]
        questions = [q for case in cases for q in case.questions]
        seed_schema = seed_schema if seed_schema is not None else spec.seed_s
        anchored = bool(seed_schema and seed_schema.strip())
        atomic_required = 'atomic_memory' in tuple(getattr(spec, 'requirements', ()))
        # A Wiki cold start can use a larger bounded correction window. Validation
        # still receives the original immutable run contract and all the same gates.
        session = ModelSession(client, replace(config, protocol_attempts=10)
                               if trial_record is not None else config,
                               'bootstrap', limit=10 if trial_record is not None else 6)
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
        if structure_sample:
            # 冻结记忆快照的结构样本（无标签）：原子记忆行样本＋节点/边类型清单——让从零
            # 生成的 S/F 匹配真实数据面。快照本体永不进提示词。
            payload['memory_structure'] = plain(structure_sample)

        async def valid(obj):
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
            from darwinagent.schema.model import Schema
            from darwinagent.operators.sandbox import admit
            schema = Schema.from_yaml(next(a.content for a in assets.assets if a.kind == 'S'))
            if schema.validate(): raise ValueError(str(schema.validate()))
            if atomic_required:
                from darwinagent.kernel.validation import atomic_memory_errors
                problems = atomic_memory_errors(schema)
                if problems: raise ValueError('S 原子记忆内核不合规: ' + str(problems))
            floor_caps = capability_names(getattr(spec, 'retrieval_floor', {}) or {})
            if floor_caps:
                from darwinagent.kernel.validation import capability_floor_errors
                problems = capability_floor_errors(assets, floor_caps)
                if problems: raise ValueError('检索工具底线不合规: ' + str(problems))
            if structure_sample:
                known_types = set(structure_sample.get('node_types') or {})
                if known_types:
                    declared = {e.name for e in schema.entities}
                    unknown = sorted(declared - known_types)
                    if unknown:
                        raise ValueError(f'S 声明了快照图中不存在的节点类型 {unknown}；'
                                         f'必须沿用 memory_structure.node_types 的既有名称')
                    if declared and schema.meta.get('atomic_memory_type') not in known_types:
                        raise ValueError('meta.atomic_memory_type 必须是快照 node_types 中的既有类型名')
            for a in assets.assets:
                if a.kind in {'F', 'C'}: admit(a.content, a.kind, [q.text for q in questions])
            if trial_graph is not None or snapshot_root is not None or trial_supply is not None:
                import tempfile
                from darwinagent.experiments.admission import AdmissionError, admit_candidate
                with tempfile.TemporaryDirectory() as trial_dir:
                    trial_bundle = assets.export(Path(trial_dir) / 'trial')
                    report_path=Path(trial_dir)/'admission.json'
                    try:
                        try:
                            if trial_supply is not None:
                                # 新模式优先：草案试跑必须在「按当前 S 重建的图」上
                                # （准入面=作答面）——snapshot_root 分支的冻结图不
                                # 代表草案真实可建图（S 声明与投影词汇不兼容在这里
                                # 即被拦，不能拖到 B0 正式准入才炸）。
                                # 抽取失败＝试验图不可用：转校验反馈让模型重拟更稳的
                                # S/草案（2026-10-05 loop1 事故同构），不放宽任何检查。
                                try:
                                    graphs=await trial_supply(trial_bundle,cases[:1])
                                except Exception as exc:
                                    raise ValueError(
                                        f'动态试验图构建失败（草案 S 声明与事实投影词汇'
                                        f'不兼容或草案过重；按结构样本声明 原子事实(键=编号)'
                                        f' 与 归属于(原子事实→人物)/属于主题/记录于 的 '
                                        f'domain/range，或简化类型与提示）: '
                                        f'{type(exc).__name__}: {str(exc)[:400]}')
                                admit_candidate(trial_bundle,cases[:1],graphs,
                                                config,floor_caps,report_path,
                                                answer_contract=getattr(spec,'answer_contract',None))
                            elif snapshot_root is not None:
                                from darwinagent.experiments.admission_worker import run_isolated
                                from darwinagent.experiments.snapshots import snapshot_digest
                                request={'bundle_path':str(trial_bundle.root),
                                         'bundle_version':trial_bundle.version,
                                         'asset_fingerprints':{
                                             a.id:a.fingerprint for a in trial_bundle.assets.assets},
                                         'snapshot_root':str(snapshot_root),
                                         'snapshot_digests':{
                                             c.id:snapshot_digest(Path(snapshot_root)/c.id)
                                             for c in cases},
                                         'cases':[c.to_dict() for c in cases],
                                         'config':config.to_dict(),
                                         'required_caps':sorted(floor_caps),
                                         'report_path':str(report_path)}
                                request_path=Path(trial_dir)/'admission-input.json'
                                atomic_json(request_path,request)
                                if not run_isolated(request_path,report_path,180):
                                    import json
                                    raise AdmissionError(report_path,json.loads(report_path.read_text()))
                            else:
                                admit_candidate(trial_bundle,cases[:1],{cases[0].id:trial_graph},
                                                config,floor_caps,report_path,
                                                answer_contract=getattr(spec,'answer_contract',None))
                        except AdmissionError as exc:
                            if trial_record is None:
                                raise
                            raise ValueError(_trial_failure_feedback(exc.report)) from exc
                    finally:
                        if trial_record is not None:
                            import json
                            trial_record(json.loads(report_path.read_text()) if report_path.exists()
                                         else {'verdict':'incomplete','candidate_version':trial_bundle.version})
            return assets
        floor_required = bool(capability_names(getattr(spec, 'retrieval_floor', {}) or {}))
        protocol = ASSET_PROTOCOL if anchored else (
            LEGACY_ASSET_PROTOCOL + ('\n' + _ATOMIC_MEMORY_CLAUSE if atomic_required else '')
            + ('\n' + _RETRIEVAL_FLOOR_CLAUSE if floor_required else '')
            + '\n' + _C_DISCIPLINE_CLAUSE)
        try:
            assets = await session.request(config.bootstrap_role, protocol, payload, valid, max_tokens=14000)
        finally:
            atomic_json(target.parent / 'bootstrap-call.json', {'input_fingerprint': digest(payload),
                         'input': payload, 'raw_outputs': session.raw, 'events': session.events})
        bundle = assets.export(target)
        validate_bundle(bundle, [q.text for q in questions])
        return bundle
