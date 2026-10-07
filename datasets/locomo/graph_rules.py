"""固定事实 → 图投影规则（新模式「冻结记忆/向量、图可重建」，recheck4 方案）。

从快照 facts.jsonl 的固定事实行确定性投影 EntityCandidate/RelationCandidate，经
`darwinagent.kg.graph.build_graph(entities, relations, schema)` 按当前 S 建图：
- 原子事实行节点：键 编号，属性 陈述/主体/类型/日期/日期粒度/日期原文/数值/主题/出处
  ——与冻结图行词表一致（向量命中映射按 编号 回行，两侧同一索引可用）；
- 人物（主体）＋`归属于` 边；主题（topics 展开）＋`属于主题` 边；会话＋`记录于` 边。
每个派生节点/边经 EntityCandidate.chunk_id 携带来源事实 ID（可追溯至固定事实）。

能力边界（如实报告，不做无限关系生成框架）：
- mentions 不携带类型信息，此投影不发 涉及活动/物品/组织/地点 与 涉及人物 边
  （旧冻结图的这些边依赖 g_v_test 侧类型判定，事实行内无此信息）；
- 人物/主题/会话 的 别名/身份/星期 等富化属性不在事实行内，投影只发键与可推导属性。
S 的改动（实体类/关系声明/主键）真实改变可建图集合：S 未声明投影词汇的候选在
build_graph 处被拒（未声明类型/属性/关系即错），S 声明即生效——规则实现版本见
PROJECTION_VERSION（进图缓存键）。
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

PROJECTION_VERSION = 'fact-projection-v2'

# 投影发出的类型与关系（S 必须声明这些才可重建）
FACT_TYPE = '原子事实'
PERSON_TYPE = '人物'
TOPIC_TYPE = '主题'
SESSION_TYPE = '会话'
REL_BELONGS = '归属于'      # 原子事实 -> 人物(主体)
REL_TOPIC = '属于主题'      # 原子事实 -> 主题
REL_SESSION = '记录于'      # 原子事实 -> 会话

WEEKDAYS = ('一', '二', '三', '四', '五', '六', '日')


def load_facts(snapshot_dir: Path):
    """固定事实行＋manifest 摘要（facts/vector 不变性的基准）。"""
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / 'manifest.json').read_text())
    rows = [json.loads(line) for line in
            (snapshot_dir / 'facts.jsonl').read_text().splitlines() if line.strip()]
    return rows, manifest


def _session_dates(facts, corpus=()):
    """会话日期来自消息元数据；事实 date_iso 是事件日期，不能作为记录日。"""
    dates = {}
    for block in _corpus_blocks(corpus):
        match = re.match(r'^D(\d+):', block.source.location)
        raw = str(block.metadata.get('date') or '')
        if not match or not raw:
            continue
        n = int(match.group(1))
        iso = date.fromisoformat(raw[:10]).isoformat()
        if n in dates and dates[n] != iso:
            raise ValueError(f'会话 {n} 的消息日期冲突')
        dates[n] = iso
    # 无记录日期时不虚构，不回退到事件日期。
    return dates


def _corpus_blocks(corpus):
    """corpus 兼容形态：Pipeline 传 str->块 映射，runner/测试传块序列。"""
    if corpus is None:
        return ()
    if hasattr(corpus, 'values'):  # Mapping
        return tuple(corpus.values())
    return tuple(corpus)


def project_candidates(facts, schema=None, corpus=()):
    """事实行 -> (entities, relations)；纯函数、零模型调用、同输入同输出。

    schema 给定时按 S 声明过滤（S 是构图规则层）：S 声明的属性/关系才进入图，
    S 未声明即不投影；但 原子事实 主键必须是 编号（向量命中映射的硬前提），
    违反直接 ValueError，不静默降级。S 声明超出事实行的属性（如 别名）不虚构。

    corpus（case.corpus 块序列）给定时，事实 出处 dia 指针逐个映射到语料块
    source id 作为 chunk_id（__sources__＝真实证据块，答题/审查证据解析用）——
    与 import_snapshots 的正源映射同构；未登记出处＝ValueError。无 corpus 时
    退回 fact:<fid> 追溯标记（离线测试用，不参与证据解析）。"""
    from darwinagent.kg.graph import EntityCandidate, RelationCandidate

    blocks = _corpus_blocks(corpus)
    dia_to_source = {b.source.location: b.source.id for b in blocks}
    chunk_for = (lambda dias: [dia_to_source[d] for d in dias]) if dia_to_source else None

    def declared(etype):
        if schema is None:
            return None
        return schema.entity(etype)

    def declared_relation(name):
        if schema is None:
            return True
        return any(r.name == name for r in schema.relations)

    def declared_props(entity_type, props):
        ent = declared(entity_type)
        if ent is None:
            return props
        allowed = {a.name for a in ent.attributes}
        aliases = getattr(ent, 'attribute_aliases', None) or getattr(ent, 'aliases', None) or {}
        allowed |= set(aliases.values()) | set(aliases.keys())
        return {k: v for k, v in props.items() if k in allowed}

    if schema is not None:
        fact_ent = declared(FACT_TYPE)
        if fact_ent is None:
            raise ValueError(f'S 未声明 {FACT_TYPE}——固定记忆事实行无处安放，拒绝重建')
        if list(fact_ent.primary_key) != ['编号']:
            raise ValueError(f'{FACT_TYPE} 主键必须是 [编号]（向量命中映射硬前提），'
                             f'当前: {list(fact_ent.primary_key)}')
    def aux_key(etype, default):
        """辅助类型（人物/主题/会话）主键跟随 S 声明（真实 bootstrap S 的 人物 键是
        名称而非冻结图 姓名——S 即规则层，投影不硬编码键名）；复合键＝S 与投影
        词汇不兼容，拒绝。schema 未给时用默认键（全量投影）。"""
        ent = declared(etype)
        if ent is None:
            return None if schema is not None else default
        if len(ent.primary_key) != 1:
            raise ValueError(f'{etype} 复合主键 {list(ent.primary_key)} 与事实行投影'
                             '不兼容——拒绝重建（不静默降级）')
        return ent.primary_key[0]

    person_key = aux_key(PERSON_TYPE, '姓名')
    topic_key = aux_key(TOPIC_TYPE, '名称')
    session_key = aux_key(SESSION_TYPE, '序号')
    emit_person = person_key is not None and declared_relation(REL_BELONGS)
    emit_topic = topic_key is not None and declared_relation(REL_TOPIC)
    emit_session = session_key is not None and declared_relation(REL_SESSION)

    entities: list[EntityCandidate] = []
    relations: list[RelationCandidate] = []
    sessions = _session_dates(facts, corpus)

    def fact_chunks(row):
        """事实的来源块 id 列表（证据解析用）；无 corpus 映射时退回追溯标记。
        sources 兼容列表与整串（conv-50 存在 "D12:12, D12:14" 单串形态，按
        importer 同款分隔规则拆分）；未登记出处＝ValueError。"""
        raw = row.get('sources') or ()
        if isinstance(raw, str):
            raw = (raw,)
        dias = [p.strip() for item in raw
                for chunk in re.split(r'[;；]', str(item))
                for p in chunk.split(',') if p.strip()]
        if chunk_for is None:
            return [f"fact:{row.get('fid', '')}"] if dias else []
        unmapped = [d for d in dias if d not in dia_to_source]
        if unmapped:
            raise ValueError(f'事实 {row.get("fid","")} 出处无法映射到语料块: {unmapped!r}')
        return [dia_to_source[d] for d in dias if d in dia_to_source]

    for row in sorted(facts, key=lambda r: str(r.get('fid', ''))):
        fid = str(row.get('fid', '')).strip()
        if not fid:
            continue
        chunks = fact_chunks(row) or ['']  # 无来源派生事实：空 chunk，构建后清空
        for chunk in chunks:
            entities.append(EntityCandidate(
                FACT_TYPE, {'编号': fid},
                declared_props(FACT_TYPE, {
                 '陈述': str(row.get('statement', '')),
                 '主体': str(row.get('subject', '')),
                 '类型': str(row.get('ftype', '')),
                 '日期': str(row.get('date_iso', '')),
                 '日期粒度': str(row.get('granularity', '')),
                 '日期原文': str(row.get('date_raw', '')),
                 '数值': str(row.get('value', '') or ''),
                 '主题': ';'.join(str(t) for t in (row.get('topics') or ())),
                 '出处': ';'.join(str(s) for s in (row.get('sources') or ()))}),
                chunk))
            subject = str(row.get('subject', '')).strip()
            if subject and emit_person:
                entities.append(EntityCandidate(
                    PERSON_TYPE, {person_key: subject},
                    declared_props(PERSON_TYPE, {}), chunk))
                relations.append(RelationCandidate(
                    REL_BELONGS, (FACT_TYPE, {'编号': fid}),
                    (PERSON_TYPE, {person_key: subject})))
            if emit_topic:
                for topic in row.get('topics') or ():
                    topic = str(topic).strip()
                    if not topic:
                        continue
                    entities.append(EntityCandidate(
                        TOPIC_TYPE, {topic_key: topic}, declared_props(TOPIC_TYPE, {}), chunk))
                    relations.append(RelationCandidate(
                        REL_TOPIC, (FACT_TYPE, {'编号': fid}),
                        (TOPIC_TYPE, {topic_key: topic})))
            n = row.get('session_no')
            if n is not None and emit_session:
                props = {}
                iso = sessions.get(n, '')
                if iso:
                    props['日期'] = iso
                    try:
                        props['星期'] = WEEKDAYS[date.fromisoformat(iso).weekday()]
                    except ValueError:
                        pass
                entities.append(EntityCandidate(
                    SESSION_TYPE, {session_key: int(n)},
                    declared_props(SESSION_TYPE, props), chunk))
                relations.append(RelationCandidate(
                    REL_SESSION, (FACT_TYPE, {'编号': fid}),
                    (SESSION_TYPE, {session_key: int(n)})))
    return entities, relations


def rebuild_graph(facts, schema, corpus=()):
    """固定事实＋当前 S -> 图（S 即构图规则：声明过滤见 project_candidates；
    编号 主键与向量映射硬前提违反即拒；corpus 给定时出处落真实证据块；
    无来源派生事实 __sources__ 为空——manifest 披露数量，不虚构来源）。"""
    from darwinagent.kg.graph import build_graph
    entities, relations = project_candidates(facts, schema, corpus)
    g = build_graph(entities, relations, schema)
    for _, nd in g.nodes(data=True):
        nd['__sources__'] = [s for s in nd.get('__sources__', ()) if s]
    g.graph['projection_version'] = PROJECTION_VERSION
    return g


def projection_structure_sample(snapshot_dir, schema=None, max_facts=30):
    """新模式 bootstrap 结构样本：与 memory_structure_sample 同形状，但词汇来自
    事实投影（原子事实/人物/主题/会话＋归属于/属于主题/记录于）——bootstrap 生成的
    S/F 草案按真实可建图的词汇起草（冻结图样本宣传的 涉及* 等边在投影能力边界外，
    照其起草的 F 会在重建图高压场景被拦）。schema 给定时按 S 过滤后的词汇计数。"""
    from collections import Counter
    snapshot_dir = Path(snapshot_dir)
    facts, manifest = load_facts(snapshot_dir)
    entities, relations = project_candidates(facts, schema)
    node_types = Counter(e.etype for e in entities)
    edge_types = Counter(r.relation for r in relations)
    samples = [{'编号': row.get('fid', ''), '陈述': str(row.get('statement', '')),
                '主体': str(row.get('subject', '')), '类型': str(row.get('ftype', '')),
                '日期': str(row.get('date_iso', '')), '日期原文': str(row.get('date_raw', '')),
                '主题': ';'.join(str(t) for t in (row.get('topics') or ())),
                '出处': ';'.join(str(s) for s in (row.get('sources') or ()))}
               for row in sorted(facts, key=lambda r: str(r.get('fid', '')))[:max_facts]]
    return {'memory_id_field': manifest.get('memory_id_field', '编号'),
            'n_facts': manifest['n_facts'], 'n_nodes': sum(node_types.values()),
            'node_types': dict(node_types), 'relations': dict(edge_types),
            'fact_samples': samples,
            'row_fields': list(dict.fromkeys(
                [k for row in samples for k in row] +
                ['node_id', 'entity_type', 'source_ids', 'claims', 'score'])),
            'row_addressing': '行由运行时字段 node_id 寻址；编号是记忆业务 id。涉及人物/物品/活动/'
                              '组织/地点 等边依赖来源侧类型判定，固定事实行内无此信息——本模式'
                              '投影不发这些边，遍历请用 归属于/属于主题/记录于 及行内字段过滤。'}


def rebuild_snapshot_graph(snapshot_dir, schema, corpus=(), embedder_factory=None):
    """Pipeline graph_builder 契约实现：固定事实＋当前 S 重建图 → 包 GraphResult
    （sources＝corpus 映射，证据解析直达语料块）→ 挂冻结向量索引 → 命中映射校验
    （任一事实行无向量记录＝硬失败，不静默降级）。"""
    from types import MappingProxyType
    import networkx as nx
    from darwinagent.contracts import GraphResult
    from darwinagent.experiments.snapshots import attach_vector
    snapshot_dir = Path(snapshot_dir)
    facts, manifest = load_facts(snapshot_dir)
    g = rebuild_graph(facts, schema, corpus)
    corpus_map = {b.source.id: b for b in _corpus_blocks(corpus)}
    graph = GraphResult(nx.freeze(g), MappingProxyType(corpus_map), (),
                        ({'stage': 'rebuilt', 'projection_version': PROJECTION_VERSION,
                          'snapshot_digest': manifest.get('snapshot_digest'),
                          'facts_digest': manifest.get('facts_digest'),
                          'nodes': g.number_of_nodes(), 'edges': g.number_of_edges()},))
    attach_vector(graph, snapshot_dir, embedder_factory=embedder_factory)
    check = vector_hit_check(graph, snapshot_dir)
    if not check['ok']:
        raise ValueError(f'向量命中映射校验失败：{len(check["missing_in_index"])} 条事实行'
                         f'无向量记录（首例 {check["missing_in_index"][:3]}）')
    return graph


def graph_cache_key(facts_digest: str, schema_fingerprint: str) -> str:
    """图缓存键：固定事实摘要＋S/规则指纹＋投影实现版本——不含题目列表、
    不含完整资产版本（F/C/P 改动复用同图；S 变才重建）。"""
    from darwinagent.runtime.artifacts import digest
    return digest({'facts': facts_digest, 'schema': schema_fingerprint,
                   'projection': PROJECTION_VERSION})


def vector_hit_check(graph, snapshot_dir: Path):
    """向量命中映射校验：每条固定事实的向量记录在索引中，且图行 编号 可回指。
    接受 GraphResult 或裸 nx 图。"""
    snapshot_dir = Path(snapshot_dir)
    index_ids = set()
    with (snapshot_dir / 'vector' / 'index.jsonl').open() as fh:
        for line in fh:
            if line.strip():
                index_ids.add(str(json.loads(line).get('id', '')))
    raw = graph if hasattr(graph, 'nodes') else graph.graph  # 裸 nx 图或 GraphResult
    row_ids = set()
    for _, nd in raw.nodes(data=True):
        if nd.get('etype') == FACT_TYPE:
            try:
                key = json.loads(nd.get('__key__', '{}'))
            except Exception:
                key = {}
            if key.get('编号'):
                row_ids.add(str(key['编号']))
    missing = sorted(row_ids - index_ids)
    return {'graph_fact_rows': len(row_ids), 'vector_records': len(index_ids),
            'missing_in_index': missing[:20], 'ok': not missing}
