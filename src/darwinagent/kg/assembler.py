"""Fact-anchored graph assembly. The structure comes from the validated memory alone:
every atomic fact is a first-class node carrying its full definition, every other node
traces back to fact nodes, and the same memory plus declared mapping yields the same graph."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

import networkx as nx

from darwinagent.contracts import AtomicFact, GraphResult, MemoryResult
from darwinagent.kg.graph import node_id
from darwinagent.runtime.artifacts import digest

CORE_TYPES = ("AtomicFact", "Entity", "Value", "Time", "EvidenceSpan", "Source")
# relation -> (domain type, range type); the fixed anchoring vocabulary.
CORE_RELATIONS = {
    "subject": ("AtomicFact", "Entity"),
    "object_entity": ("AtomicFact", "Entity"),
    "object_value": ("AtomicFact", "Value"),
    "occurrence_time": ("AtomicFact", "Time"),
    "evidence": ("AtomicFact", "EvidenceSpan"),
    "locates": ("EvidenceSpan", "Source"),
    "time_anchor": ("Time", "Source"),
}


def anchoring_errors(schema) -> list[str]:
    """The frozen task-modeling constraint: S may extend but never drop the anchor."""
    errors = []
    for name in CORE_TYPES:
        if schema.entity(name) is None:
            errors.append(f"锚定节点类型缺失: {name}")
    for name, (dom, rng) in CORE_RELATIONS.items():
        rel = next((r for r in schema.relations if r.name == name), None)
        if rel is None:
            errors.append(f"锚定关系缺失: {name}")
        elif dom not in rel.domain or rel.range != rng:
            errors.append(f"锚定关系 {name} 的 domain/range 被改动（须为 {dom}->{rng}）")
    classes = schema.meta.get("entity_classes")
    if (
        not isinstance(classes, list)
        or not classes
        or not all(type(x) is str and x.strip() for x in classes)
    ):
        errors.append("meta.entity_classes 未声明实体分类")
    return errors


def recover_facts(graph) -> list[dict]:
    """The memory, read back out of the graph's fact nodes."""
    rows = []
    for _, nd in graph.nodes(data=True):
        if nd.get("etype") == "AtomicFact":
            rows.append(json.loads(nd["__fact__"]))
    return sorted(rows, key=lambda x: x["id"])


def materialization_plan(facts, schema):
    """Deterministic view derivation shared by assembly and validation: typed rows rebuilt
    from positive-statement fact predicates. Returns ({view_node_id: (etype, key, attrs,
    frozenset(fact_ids))}, skipped_count)."""
    views = schema.meta.get("materialized") or {}
    if not isinstance(views, dict):
        raise ValueError("materialized 声明必须是 {视图类型: {entity_class: 类别}}")
    if views and "materialized_from" not in {r.name for r in schema.relations}:
        raise ValueError("物化视图需要声明 materialized_from 关系（视图 -> AtomicFact）")
    from darwinagent.kg.graph import _typed

    plan = {}
    skipped = 0
    for view_type, declaration in sorted(views.items()):
        view_spec = schema.entity(view_type)
        if (
            view_spec is None
            or not isinstance(declaration, dict)
            or "entity_class" not in declaration
        ):
            raise ValueError(f"物化视图声明不完整: {view_type}")
        dtypes = {a.name: a.dtype for a in view_spec.attributes}
        subjects = {}
        for fact in sorted(facts, key=lambda f: f.id):
            # Only unconditional positive statements materialize into typed views:
            # negation, plans and hypotheses stay as facts and never overwrite attributes.
            if (
                fact.object_value is None
                or fact.polarity != "positive"
                or fact.modality != "statement"
            ):
                continue
            subjects.setdefault(
                node_id("Entity", {"class": fact.subject.cls, "name": fact.subject.name}), []
            ).append(fact)
        for entity_nid in sorted(subjects):
            head = subjects[entity_nid][0]
            if head.subject.cls != declaration["entity_class"]:
                continue
            collected = {}
            for fact in subjects[entity_nid]:
                if fact.predicate not in dtypes:
                    continue
                try:
                    value = _typed(fact.object_value.value, dtypes[fact.predicate])
                except Exception:
                    skipped += 1
                    continue
                collected.setdefault(fact.predicate, []).append((fact, value))
            attrs = {}
            contributing = []
            for predicate, pairs in sorted(collected.items()):
                if len({v for _, v in pairs}) > 1:
                    skipped += 1  # conflicting evidenced values: no silent pick
                    continue
                attrs[predicate] = pairs[0][1]
                contributing.extend(f for f, _ in pairs)
            if not attrs or not contributing:
                continue
            key = {k: attrs.get(k) for k in view_spec.primary_key}
            if any(v is None or v == "" for v in key.values()):
                skipped += 1
                continue
            plan[node_id(view_type, key)] = (
                view_type,
                key,
                attrs,
                frozenset(f.id for f in contributing),
            )
    return plan, skipped


def anchoring_invariants(
    graph, sources: Mapping, expected_fingerprint: str | None = None, schema=None
) -> list[str]:
    errors = []
    facts = []
    fact_objs = []
    expected_sources = {}
    fact_out_relations = ("subject", "object_entity", "object_value", "occurrence_time", "evidence")
    # Edge label integrity: the multigraph key and the relation attribute that queries read
    # must name the same relation on every edge, or structure checks and traversal diverge.
    for h, t, key, ed in graph.edges(keys=True, data=True):
        if ed.get("relation") != key:
            errors.append(f"边标签不一致: key={key!r} 而 relation 属性为 {ed.get('relation')!r}")
    # Source node content must match the registered corpus metadata exactly.
    by_ref = {(b.source.kind, b.source.document_id, b.source.location): b for b in sources.values()}
    for nid, nd in graph.nodes(data=True):
        if nd.get("etype") != "Source":
            continue
        block = by_ref.get((nd.get("kind"), nd.get("document_id"), nd.get("location")))
        if block is None:
            errors.append(f"来源节点 {nid[:48]} 未注册于语料")
        elif nd.get("speaker") != str(block.metadata.get("speaker", "")) or nd.get("date") != str(
            block.metadata.get("date", "")
        ):
            errors.append(f"来源节点 {nid[:48]} 的说话人/日期与语料元数据不一致")
    for nid, nd in graph.nodes(data=True):
        if nd.get("etype") != "AtomicFact":
            continue
        try:
            fact = AtomicFact.from_dict(json.loads(nd["__fact__"]))
        except Exception as exc:
            errors.append(f"事实节点 {nid} 定义不完整: {exc}")
            continue
        # The retrievable node attributes must say exactly what __fact__ defines;
        # leaving __fact__ intact while rewriting visible attributes is a violation.
        if any(
            nd.get(field) != getattr(fact, field)
            for field in ("id", "text", "predicate", "polarity", "modality")
        ):
            errors.append(f"事实节点 {nid} 的可检索属性与 __fact__ 定义不一致")
        for ev in fact.evidence:
            block = sources.get(ev.source_id)
            if block is None or block.text[ev.start : ev.end] != ev.quote:
                errors.append(f"事实节点 {nid} 的证据偏移与来源原文不符")
        # Structural cross-check: every core edge of the fact must point at exactly the
        # nodes its definition names — swapped subject links or rewritten Time/EvidenceSpan
        # content are violations even when the embedded __fact__ still matches.
        expected = {
            "subject": {node_id("Entity", {"class": fact.subject.cls, "name": fact.subject.name})},
            "object_entity": (
                {
                    node_id(
                        "Entity", {"class": fact.object_entity.cls, "name": fact.object_entity.name}
                    )
                }
                if fact.object_entity
                else set()
            ),
            "object_value": (
                {
                    node_id(
                        "Value",
                        {"dtype": fact.object_value.dtype, "value": fact.object_value.value},
                    )
                }
                if fact.object_value
                else set()
            ),
            "occurrence_time": {node_id("Time", {"id": digest(fact.time.to_dict())})},
            "evidence": {
                node_id(
                    "EvidenceSpan",
                    {
                        "id": digest(
                            {
                                "source_id": ev.source_id,
                                "quote": ev.quote,
                                "start": ev.start,
                                "end": ev.end,
                            }
                        )
                    },
                )
                for ev in fact.evidence
            },
        }
        for relation in fact_out_relations:
            actual = {
                target
                for _, target, ed in graph.out_edges(nid, data=True)
                if ed.get("relation") == relation
            }
            if actual != expected[relation]:
                errors.append(f"事实节点 {nid} 的 {relation} 连边与其定义不一致")
        time_nid = node_id("Time", {"id": digest(fact.time.to_dict())})
        if graph.has_node(time_nid):
            tnd = graph.nodes[time_nid]
            for field, value in (
                ("raw", fact.time.raw),
                ("precision", fact.time.precision),
                ("start", fact.time.start),
                ("end", fact.time.end),
                ("anchor_source_id", fact.time.anchor_source_id),
            ):
                if tnd.get(field) != value:
                    errors.append(f"时间节点 {time_nid[:48]} 的 {field} 与事实定义不一致")
            block = sources.get(fact.time.anchor_source_id)
            want = (
                {
                    node_id(
                        "Source",
                        {
                            "kind": block.source.kind,
                            "document_id": block.source.document_id,
                            "location": block.source.location,
                        },
                    )
                }
                if fact.time.anchor_source_id
                else set()
            )
            got = {
                target
                for _, target, ed in graph.out_edges(time_nid, data=True)
                if ed.get("relation") == "time_anchor"
            }
            if got != want:
                errors.append("时间节点锚点连边与事实定义不一致")
        for ev in fact.evidence:
            span_nid = node_id(
                "EvidenceSpan",
                {
                    "id": digest(
                        {
                            "source_id": ev.source_id,
                            "quote": ev.quote,
                            "start": ev.start,
                            "end": ev.end,
                        }
                    )
                },
            )
            if graph.has_node(span_nid):
                snd = graph.nodes[span_nid]
                if (
                    snd.get("source_id"),
                    snd.get("quote"),
                    snd.get("start_offset"),
                    snd.get("end_offset"),
                ) != (ev.source_id, ev.quote, ev.start, ev.end):
                    errors.append("证据节点内容与事实定义不一致")
                block = sources.get(ev.source_id)
                want = node_id(
                    "Source",
                    {
                        "kind": block.source.kind,
                        "document_id": block.source.document_id,
                        "location": block.source.location,
                    },
                )
                got = {
                    target
                    for _, target, ed in graph.out_edges(span_nid, data=True)
                    if ed.get("relation") == "locates"
                }
                if got != {want}:
                    errors.append("证据定位连边与事实定义不一致")
        facts.append(fact.to_dict())
        fact_objs.append(fact)
        # Provenance follows structure: each node's __sources__ must be exactly the evidence
        # of the facts (or spans/views/registrations) that place it in the graph.
        ev_sources = {ev.source_id for ev in fact.evidence}
        expected_sources.setdefault(nid, set()).update(ev_sources)
        for relation in ("subject", "object_entity", "object_value", "occurrence_time"):
            for t_nid in expected[relation]:
                expected_sources.setdefault(t_nid, set()).update(ev_sources)
        for ev in fact.evidence:
            expected_sources.setdefault(
                node_id(
                    "EvidenceSpan",
                    {
                        "id": digest(
                            {
                                "source_id": ev.source_id,
                                "quote": ev.quote,
                                "start": ev.start,
                                "end": ev.end,
                            }
                        )
                    },
                ),
                set(),
            ).add(ev.source_id)
    if not facts:
        errors.append("图中没有记忆节点")
    if schema is not None and schema.meta.get("materialized"):
        plan, _ = materialization_plan(fact_objs, schema)
        view_types = set(schema.meta["materialized"])
        actual_views = {
            nid: nd for nid, nd in graph.nodes(data=True) if nd.get("etype") in view_types
        }
        from darwinagent.kg.graph import node_view

        for nid in sorted(set(plan) | set(actual_views)):
            if nid not in actual_views:
                errors.append(f"物化视图缺失: {nid[:64]}")
                continue
            if nid not in plan:
                errors.append(f"物化视图不可由事实推导: {nid[:64]}")
                continue
            _, key, attrs, fact_ids = plan[nid]
            planned = {**key, **attrs}
            runtime_meta = {"node_id", "entity_type", "source_ids", "claims"}
            actual = {
                k: v for k, v in node_view(actual_views[nid]).items() if k not in runtime_meta
            }
            if set(actual) != set(planned):
                extra = sorted(set(actual) - set(planned))
                missing = sorted(set(planned) - set(actual))
                errors.append(
                    f"物化视图 {nid[:64]} 属性集与事实推导不符（多余: {extra} 缺失: {missing}）"
                )
                continue
            for field, value in planned.items():
                if actual[field] != value:
                    errors.append(f"物化视图 {nid[:64]} 的 {field} 与事实推导不一致")
            want_edges = {node_id("AtomicFact", {"id": fid}) for fid in fact_ids}
            got_edges = {
                target
                for _, target, ed in graph.out_edges(nid, data=True)
                if ed.get("relation") == "materialized_from"
            }
            if got_edges != want_edges:
                errors.append(f"物化视图 {nid[:64]} 的 materialized_from 连边与事实不一致")

    for nid, nd in graph.nodes(data=True):
        if nd.get("etype") == "Source":
            block = by_ref.get((nd.get("kind"), nd.get("document_id"), nd.get("location")))
            if block is not None:
                expected_sources.setdefault(nid, set()).add(block.source.id)
    if schema is not None and schema.meta.get("materialized"):
        for view_nid, (_, _, _, fact_ids) in plan.items():
            union = set()
            for f in fact_objs:
                if f.id in fact_ids:
                    union.update(ev.source_id for ev in f.evidence)
            expected_sources.setdefault(view_nid, set()).update(union)
    for nid, nd in graph.nodes(data=True):
        if nd.get("etype") in (
            "AtomicFact",
            "Entity",
            "Value",
            "Time",
            "EvidenceSpan",
            "Source",
        ) or (
            schema is not None
            and schema.meta.get("materialized")
            and nd.get("etype") in schema.meta["materialized"]
        ):
            actual = set(nd.get("__sources__", []))
            want = expected_sources.get(nid)
            if want is None or actual != want:
                errors.append(
                    f"节点 {nid[:56]} 的来源追踪与结构不符（__sources__ 应为 {sorted(want) if want else '无依据'}）"
                )

    undirected = nx.Graph()
    undirected.add_nodes_from(graph.nodes)
    undirected.add_edges_from((h, t) for h, t in graph.edges(keys=False))
    fact_nodes = {nid for nid, nd in graph.nodes(data=True) if nd.get("etype") == "AtomicFact"}
    for component in nx.connected_components(undirected):
        if not (component & fact_nodes):
            errors.append(f"游离结构（无法回溯记忆节点）: {sorted(component)[:3]}")
    stored = graph.graph.get("memory_fingerprint")
    recovered = digest(sorted(facts, key=lambda x: x["id"])) if facts else ""
    if stored and stored != recovered:
        errors.append("图上复原的记忆与记忆指纹不一致")
    if expected_fingerprint is not None and recovered != expected_fingerprint:
        errors.append("图未能完整复原原子记忆（round-trip 指纹不一致）")
    return errors


class GraphAssembler:
    """Deterministic assembly from a MemoryResult under the schema's declared mapping."""

    @staticmethod
    def build(memory: MemoryResult, spec, schema) -> GraphResult:
        problems = anchoring_errors(schema)
        if problems:
            raise ValueError("Schema 缺少事实锚定声明: " + "; ".join(problems))
        g = nx.MultiDiGraph()
        g.graph.update(format_version=3, memory_fingerprint=memory.fingerprint)

        def node(etype, key, **attrs):
            nid = node_id(etype, key)
            if nid not in g:
                g.add_node(
                    nid,
                    etype=etype,
                    __key__=json.dumps(key, ensure_ascii=False),
                    __merged__=0,
                    __sources__=[],
                    **attrs,
                )
            return nid

        def touch(nid, source_ids):
            nd = g.nodes[nid]
            merged = set(nd["__sources__"]) | set(source_ids)
            nd["__sources__"] = sorted(merged)
            nd["__merged__"] += 1

        def link(head, tail, relation):
            if not g.has_edge(head, tail, key=relation):
                g.add_edge(head, tail, key=relation, relation=relation)

        def source_node(block):
            return node(
                "Source",
                {
                    "kind": block.source.kind,
                    "document_id": block.source.document_id,
                    "location": block.source.location,
                },
                kind=block.source.kind,
                document_id=block.source.document_id,
                location=block.source.location,
                speaker=str(block.metadata.get("speaker", "")),
                date=str(block.metadata.get("date", "")),
            )

        counts = {name: 0 for name in CORE_TYPES}
        for fact in sorted(memory.facts, key=lambda f: f.id):
            evidence_sources = sorted({ev.source_id for ev in fact.evidence})
            for ev in fact.evidence:
                block = memory.corpus.get(ev.source_id)
                if block is None or block.text[ev.start : ev.end] != ev.quote:
                    raise ValueError(f"事实 {fact.id} 的证据在来源 {ev.source_id} 上定位失败")
            fact_node = node(
                "AtomicFact",
                {"id": fact.id},
                id=fact.id,
                text=fact.text,
                predicate=fact.predicate,
                polarity=fact.polarity,
                modality=fact.modality,
                __fact__=json.dumps(fact.to_dict(), ensure_ascii=False, sort_keys=True),
            )
            touch(fact_node, evidence_sources)
            subject = node(
                "Entity",
                {"class": fact.subject.cls, "name": fact.subject.name},
                **{"class": fact.subject.cls, "name": fact.subject.name},
            )
            touch(subject, evidence_sources)
            link(fact_node, subject, "subject")
            if fact.object_entity is not None:
                target = node(
                    "Entity",
                    {"class": fact.object_entity.cls, "name": fact.object_entity.name},
                    **{"class": fact.object_entity.cls, "name": fact.object_entity.name},
                )
                touch(target, evidence_sources)
                link(fact_node, target, "object_entity")
            if fact.object_value is not None:
                value = node(
                    "Value",
                    {"dtype": fact.object_value.dtype, "value": fact.object_value.value},
                    dtype=fact.object_value.dtype,
                    value=fact.object_value.value,
                )
                touch(value, evidence_sources)
                link(fact_node, value, "object_value")
            time = fact.time
            time_node = node(
                "Time",
                {"id": digest(time.to_dict())},
                raw=time.raw,
                precision=time.precision,
                start=time.start,
                end=time.end,
                anchor_source_id=time.anchor_source_id,
            )
            touch(time_node, evidence_sources)
            link(fact_node, time_node, "occurrence_time")
            if time.anchor_source_id:
                anchor_block = memory.corpus.get(time.anchor_source_id)
                if anchor_block is None:
                    raise ValueError(
                        f"事实 {fact.id} 的时间锚点来源未注册: {time.anchor_source_id}"
                    )
                anchor = source_node(anchor_block)
                touch(anchor, [time.anchor_source_id])
                link(time_node, anchor, "time_anchor")
            for ev in fact.evidence:
                span = node(
                    "EvidenceSpan",
                    {
                        "id": digest(
                            {
                                "source_id": ev.source_id,
                                "quote": ev.quote,
                                "start": ev.start,
                                "end": ev.end,
                            }
                        )
                    },
                    source_id=ev.source_id,
                    quote=ev.quote,
                    start_offset=ev.start,
                    end_offset=ev.end,
                )
                touch(span, [ev.source_id])
                link(fact_node, span, "evidence")
                block = memory.corpus[ev.source_id]
                src = source_node(block)
                touch(src, [ev.source_id])
                link(span, src, "locates")

        # Declared materialized views: typed rows rebuilt from fact predicates, traceable to facts.
        plan, skipped_views = materialization_plan(sorted(memory.facts, key=lambda f: f.id), schema)
        for view_nid, (view_type, key, attrs, fact_ids) in sorted(plan.items()):
            view_nid = node(view_type, key, **attrs)
            touch(
                view_nid,
                sorted(
                    {ev.source_id for f in memory.facts if f.id in fact_ids for ev in f.evidence}
                ),
            )
            for fid in sorted(fact_ids):
                link(view_nid, node_id("AtomicFact", {"id": fid}), "materialized_from")
        for nid, nd in g.nodes(data=True):
            counts[nd["etype"]] = counts.get(nd["etype"], 0) + 1
        violations = anchoring_invariants(g, memory.corpus, memory.fingerprint, schema)
        if violations:
            raise ValueError("装配违反事实锚定不变量: " + "; ".join(violations))
        diagnostics = (
            {
                "stage": "assembled",
                "memory_fingerprint": memory.fingerprint,
                "nodes": g.number_of_nodes(),
                "edges": g.number_of_edges(),
                "by_type": counts,
                "skipped_view_materializations": skipped_views,
            },
        )
        return GraphResult(nx.freeze(g), MappingProxyType(dict(memory.corpus)), (), diagnostics)
