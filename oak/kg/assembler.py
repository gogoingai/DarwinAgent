"""Fact-anchored graph assembly. The structure comes from the validated memory alone:
every atomic fact is a first-class node carrying its full definition, every other node
traces back to fact nodes, and the same memory plus declared mapping yields the same graph."""
from __future__ import annotations

import json
from typing import Mapping

import networkx as nx
from types import MappingProxyType

from oak.contracts import AtomicFact, GraphResult, MemoryResult
from oak.kg.graph import node_id
from oak.runtime.artifacts import digest

CORE_TYPES = ('AtomicFact', 'Entity', 'Value', 'Time', 'EvidenceSpan', 'Source')
# relation -> (domain type, range type); the fixed anchoring vocabulary.
CORE_RELATIONS = {'subject': ('AtomicFact', 'Entity'), 'object_entity': ('AtomicFact', 'Entity'),
                  'object_value': ('AtomicFact', 'Value'), 'occurrence_time': ('AtomicFact', 'Time'),
                  'evidence': ('AtomicFact', 'EvidenceSpan'), 'locates': ('EvidenceSpan', 'Source'),
                  'time_anchor': ('Time', 'Source')}


def anchoring_errors(schema) -> list[str]:
    """The frozen task-modeling constraint: S may extend but never drop the anchor."""
    errors = []
    for name in CORE_TYPES:
        if schema.entity(name) is None:
            errors.append(f'锚定节点类型缺失: {name}')
    for name, (dom, rng) in CORE_RELATIONS.items():
        rel = next((r for r in schema.relations if r.name == name), None)
        if rel is None:
            errors.append(f'锚定关系缺失: {name}')
        elif dom not in rel.domain or rel.range != rng:
            errors.append(f'锚定关系 {name} 的 domain/range 被改动（须为 {dom}->{rng}）')
    classes = schema.meta.get('entity_classes')
    if not isinstance(classes, list) or not classes or not all(type(x) is str and x.strip() for x in classes):
        errors.append('meta.entity_classes 未声明实体分类')
    return errors


def recover_facts(graph) -> list[dict]:
    """The memory, read back out of the graph's fact nodes."""
    rows = []
    for _, nd in graph.nodes(data=True):
        if nd.get('etype') == 'AtomicFact':
            rows.append(json.loads(nd['__fact__']))
    return sorted(rows, key=lambda x: x['id'])


def anchoring_invariants(graph, sources: Mapping, expected_fingerprint: str | None = None) -> list[str]:
    errors = []
    facts = []
    for nid, nd in graph.nodes(data=True):
        if nd.get('etype') != 'AtomicFact':
            continue
        try:
            fact = AtomicFact.from_dict(json.loads(nd['__fact__']))
        except Exception as exc:
            errors.append(f'事实节点 {nid} 定义不完整: {exc}')
            continue
        for ev in fact.evidence:
            block = sources.get(ev.source_id)
            if block is None or block.text[ev.start:ev.end] != ev.quote:
                errors.append(f'事实节点 {nid} 的证据偏移与来源原文不符')
        facts.append(fact.to_dict())
    if not facts:
        errors.append('图中没有记忆节点')
    undirected = nx.Graph()
    undirected.add_nodes_from(graph.nodes)
    undirected.add_edges_from((h, t) for h, t in graph.edges(keys=False))
    fact_nodes = {nid for nid, nd in graph.nodes(data=True) if nd.get('etype') == 'AtomicFact'}
    for component in nx.connected_components(undirected):
        if not (component & fact_nodes):
            errors.append(f'游离结构（无法回溯记忆节点）: {sorted(component)[:3]}')
    stored = graph.graph.get('memory_fingerprint')
    recovered = digest(sorted(facts, key=lambda x: x['id'])) if facts else ''
    if stored and stored != recovered:
        errors.append('图上复原的记忆与记忆指纹不一致')
    if expected_fingerprint is not None and recovered != expected_fingerprint:
        errors.append('图未能完整复原原子记忆（round-trip 指纹不一致）')
    return errors


class GraphAssembler:
    """Deterministic assembly from a MemoryResult under the schema's declared mapping."""

    @staticmethod
    def build(memory: MemoryResult, spec, schema) -> GraphResult:
        problems = anchoring_errors(schema)
        if problems:
            raise ValueError('Schema 缺少事实锚定声明: ' + '; '.join(problems))
        g = nx.MultiDiGraph()
        g.graph.update(format_version=3, memory_fingerprint=memory.fingerprint)

        def node(etype, key, **attrs):
            nid = node_id(etype, key)
            if nid not in g:
                g.add_node(nid, etype=etype, __key__=json.dumps(key, ensure_ascii=False),
                           __merged__=0, __sources__=[], **attrs)
            return nid

        def touch(nid, source_ids):
            nd = g.nodes[nid]
            merged = set(nd['__sources__']) | set(source_ids)
            nd['__sources__'] = sorted(merged)
            nd['__merged__'] += 1

        def link(head, tail, relation):
            if not g.has_edge(head, tail, key=relation):
                g.add_edge(head, tail, key=relation, relation=relation)

        def source_node(block):
            return node('Source', {'kind': block.source.kind, 'document_id': block.source.document_id,
                                   'location': block.source.location},
                        speaker=str(block.metadata.get('speaker', '')),
                        date=str(block.metadata.get('date', '')))

        counts = {name: 0 for name in CORE_TYPES}
        for fact in sorted(memory.facts, key=lambda f: f.id):
            evidence_sources = sorted({ev.source_id for ev in fact.evidence})
            for ev in fact.evidence:
                block = memory.corpus.get(ev.source_id)
                if block is None or block.text[ev.start:ev.end] != ev.quote:
                    raise ValueError(f'事实 {fact.id} 的证据在来源 {ev.source_id} 上定位失败')
            fact_node = node('AtomicFact', {'id': fact.id}, id=fact.id, text=fact.text,
                             predicate=fact.predicate, polarity=fact.polarity, modality=fact.modality,
                             __fact__=json.dumps(fact.to_dict(), ensure_ascii=False, sort_keys=True))
            touch(fact_node, evidence_sources)
            subject = node('Entity', {'class': fact.subject.cls, 'name': fact.subject.name},
                           **{'class': fact.subject.cls, 'name': fact.subject.name})
            touch(subject, evidence_sources); link(fact_node, subject, 'subject')
            if fact.object_entity is not None:
                target = node('Entity', {'class': fact.object_entity.cls, 'name': fact.object_entity.name},
                              **{'class': fact.object_entity.cls, 'name': fact.object_entity.name})
                touch(target, evidence_sources); link(fact_node, target, 'object_entity')
            if fact.object_value is not None:
                value = node('Value', {'dtype': fact.object_value.dtype, 'value': fact.object_value.value},
                             dtype=fact.object_value.dtype, value=fact.object_value.value)
                touch(value, evidence_sources); link(fact_node, value, 'object_value')
            time = fact.time
            time_node = node('Time', {'id': digest(time.to_dict())}, raw=time.raw, precision=time.precision,
                             start=time.start, end=time.end, anchor_source_id=time.anchor_source_id)
            touch(time_node, evidence_sources); link(fact_node, time_node, 'occurrence_time')
            if time.anchor_source_id:
                anchor_block = memory.corpus.get(time.anchor_source_id)
                if anchor_block is None:
                    raise ValueError(f'事实 {fact.id} 的时间锚点来源未注册: {time.anchor_source_id}')
                anchor = source_node(anchor_block); touch(anchor, [time.anchor_source_id])
                link(time_node, anchor, 'time_anchor')
            for ev in fact.evidence:
                span = node('EvidenceSpan', {'id': digest({'source_id': ev.source_id, 'quote': ev.quote,
                                                           'start': ev.start, 'end': ev.end})},
                            source_id=ev.source_id, quote=ev.quote, start_offset=ev.start, end_offset=ev.end)
                touch(span, [ev.source_id]); link(fact_node, span, 'evidence')
                block = memory.corpus[ev.source_id]
                src = source_node(block); touch(src, [ev.source_id]); link(span, src, 'locates')

        # Declared materialized views: typed rows rebuilt from fact predicates, traceable to facts.
        views = schema.meta.get('materialized') or {}
        if views and not isinstance(views, dict):
            raise ValueError('materialized 声明必须是 {视图类型: {entity_class: 类别}}')
        from oak.kg.graph import _typed
        skipped_views = 0
        for view_type, declaration in sorted(views.items()):
            view_spec = schema.entity(view_type)
            if view_spec is None or not isinstance(declaration, dict) or 'entity_class' not in declaration:
                raise ValueError(f'物化视图声明不完整: {view_type}')
            if 'materialized_from' not in {r.name for r in schema.relations}:
                raise ValueError('物化视图需要声明 materialized_from 关系（视图 -> AtomicFact）')
            dtypes = {a.name: a.dtype for a in view_spec.attributes}
            subjects = {}
            for fact in sorted(memory.facts, key=lambda f: f.id):
                if fact.object_value is None:
                    continue
                subjects.setdefault(node_id('Entity', {'class': fact.subject.cls, 'name': fact.subject.name}), []).append(fact)
            for entity_nid in sorted(subjects):
                entity = g.nodes.get(entity_nid)
                if entity is None or entity.get('class') != declaration['entity_class']:
                    continue
                attrs = {}; contributing = []
                for fact in subjects[entity_nid]:
                    if fact.predicate not in dtypes:
                        continue
                    try:
                        attrs[fact.predicate] = _typed(fact.object_value.value, dtypes[fact.predicate])
                    except Exception:
                        skipped_views += 1; continue
                    contributing.append(fact)
                if not contributing:
                    continue
                key = {k: attrs.get(k) for k in view_spec.primary_key}
                if any(v is None or v == '' for v in key.values()):
                    skipped_views += 1; continue
                view_nid = node(view_type, key, **attrs)
                touch(view_nid, sorted({ev.source_id for f in contributing for ev in f.evidence}))
                for fact in contributing:
                    link(view_nid, node_id('AtomicFact', {'id': fact.id}), 'materialized_from')
        for nid, nd in g.nodes(data=True):
            counts[nd['etype']] = counts.get(nd['etype'], 0) + 1
        violations = anchoring_invariants(g, memory.corpus, memory.fingerprint)
        if violations:
            raise ValueError('装配违反事实锚定不变量: ' + '; '.join(violations))
        diagnostics = ({'stage': 'assembled', 'memory_fingerprint': memory.fingerprint,
                        'nodes': g.number_of_nodes(), 'edges': g.number_of_edges(), 'by_type': counts,
                        'skipped_view_materializations': skipped_views},)
        return GraphResult(nx.freeze(g), MappingProxyType(dict(memory.corpus)), (), diagnostics)
