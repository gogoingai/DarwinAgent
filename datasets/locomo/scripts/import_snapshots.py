"""Import the frozen g_v_test memory builds (per-conversation facts + graph + vector index)
into Oak snapshot directories, one build per conversation, shared by every experiment arm.

Source of truth stays in g_v_test (pinned build config); this importer only COPIES and
rewrites source attribution to message level:
  - 原子事实 nodes: 出处 dia_id -> the adapter corpus SourceRef id (message-level evidence);
  - derived nodes (主体/涉及/主题/会话): union of the sources of the facts that connect to
    them (their structural reason to exist);
then records digests in manifest.json. Deterministic: same inputs -> byte-identical snapshot.

Usage: uv run python -m datasets.locomo.scripts.import_snapshots \
         --source /Users/xu/git/memory-schema-rsi/g_v_test --convs conv-26,conv-30,...
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

from oak.kg.graph import load_graph, save_graph
from oak.runtime.artifacts import digest

from datasets.locomo.adapter import LocomoAdapter

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / 'datasets/locomo/snapshots/gvtest_v1'


def graph_dir_for(source: Path, conv: str) -> Path:
    candidates = sorted((source / 'runs' / conv).glob('graph_*'))
    if not candidates:
        raise ValueError(f'{conv}: g_v_test 无冻结图（runs/{conv}/graph_*）')
    if len(candidates) > 1:
        raise ValueError(f'{conv}: 多个图目录，需人工指定：{[c.name for c in candidates]}')
    return candidates[0]


def import_conv(source: Path, out_root: Path, conv: str, adapter: LocomoAdapter) -> dict:
    src = graph_dir_for(source, conv)
    out = out_root / conv
    if out.exists():
        raise ValueError(f'快照已存在（先删除再导入）：{out}')
    out.mkdir(parents=True)

    case = adapter.generation_input(conv)
    dia_to_source = {b.source.location: b.source.id for b in case.corpus}
    valid_source_ids = set(dia_to_source.values())

    graph = load_graph(src / 'graph.json')
    facts = [json.loads(line) for line in (src / 'facts.jsonl').read_text().splitlines() if line.strip()]
    n_nodes = graph.number_of_nodes()

    # 1) 原子事实：出处 dia_id -> 消息级 SourceRef id
    fact_nodes = 0
    for _nid, nd in graph.nodes(data=True):
        if nd.get('etype') != '原子事实':
            continue
        fact_nodes += 1
        dias = [d for d in re.split(r'[;；，,]', str(nd.get('出处', ''))) if d]
        mapped = [dia_to_source[d] for d in dias if d in dia_to_source]
        if not mapped:
            raise ValueError(f'{conv}: 事实 {nd.get("编号")} 出处无法映射到语料块：{nd.get("出处")!r}')
        nd['__sources__'] = mapped
    if fact_nodes != len(facts):
        raise ValueError(f'{conv}: 图中原子事实 {fact_nodes} != facts.jsonl {len(facts)}')

    # 2) 派生节点（主体/涉及/主题/会话）：连接到它的事实来源并集
    derived_sources = defaultdict(set)
    for u, v, _ed in graph.edges(data=True):
        und = graph.nodes[u]
        if und.get('etype') == '原子事实':
            derived_sources[v].update(und.get('__sources__', []))
    for nid, nd in graph.nodes(data=True):
        if nd.get('etype') == '原子事实':
            continue
        nd['__sources__'] = sorted(derived_sources.get(nid, set()))
    unknown = {s for _n, nd in graph.nodes(data=True) for s in nd.get('__sources__', [])} - valid_source_ids
    if unknown:
        raise ValueError(f'{conv}: 改写后仍含未登记来源：{sorted(unknown)[:5]}')
    empty = [n for n, nd in graph.nodes(data=True) if not nd.get('__sources__')]
    if empty:
        raise ValueError(f'{conv}: {len(empty)} 个节点无来源（孤立结构不允许）：{empty[:3]}')

    # 3) 落盘：图（改写后）、事实、向量索引、嵌入缓存
    save_graph(graph, out / 'graph.json')
    graph_digest = digest(json.loads((out / 'graph.json').read_text()))
    shutil.copyfile(src / 'facts.jsonl', out / 'facts.jsonl')
    facts_digest = digest([json.loads(line) for line in (out / 'facts.jsonl').read_text().splitlines() if line.strip()])
    vec_src = source / 'data/vecstore' / f'facts_store_{conv}.jsonl'
    if not vec_src.exists():
        raise ValueError(f'{conv}: 向量索引缺失：{vec_src}')
    (out / 'vector').mkdir()
    shutil.copyfile(vec_src, out / 'vector' / 'index.jsonl')
    vector_digest = digest([json.loads(line) for line in (out / 'vector' / 'index.jsonl').read_text().splitlines() if line.strip()])
    for extra in ('embed_cache.json',):
        if (source / 'data/vecstore' / extra).exists():
            shutil.copyfile(source / 'data/vecstore' / extra, out / 'vector' / extra)
    n_vector = sum(1 for line in (out / 'vector' / 'index.jsonl').read_text().splitlines() if line.strip())
    fact_ids = {str(f.get('fid')) for f in facts}
    memory_ids = {str(nd.get('编号')) for _n, nd in graph.nodes(data=True) if nd.get('etype') == '原子事实'}
    if fact_ids != memory_ids:
        raise ValueError(f'{conv}: facts.jsonl 与图节点编号集不一致：{sorted(fact_ids ^ memory_ids)[:5]}')
    vector_ids = {json.loads(line)['id'] for line in (out / 'vector' / 'index.jsonl').read_text().splitlines() if line.strip()}
    if not vector_ids <= memory_ids:
        raise ValueError(f'{conv}: 向量索引含图外记忆 id：{sorted(vector_ids - memory_ids)[:5]}')
    if n_vector < fact_nodes:
        raise ValueError(f'{conv}: 向量记录 {n_vector} 少于事实数 {fact_nodes}')

    meta_src = source / 'data/vecstore' / f'facts_store_{conv}_meta.json'
    embed_meta = json.loads(meta_src.read_text()) if meta_src.exists() else {}
    manifest = {'conversation': conv, 'source_graph': str(src.relative_to(source)),
                'n_facts': len(facts), 'n_nodes': n_nodes, 'n_vector_records': n_vector,
                'memory_id_field': '编号', 'graph_digest': graph_digest,
                'facts_digest': facts_digest, 'vector_digest': vector_digest,
                'embed': embed_meta.get('embed', {}), 'dia_coverage': len(dia_to_source)}
    manifest['snapshot_digest'] = digest({k: v for k, v in manifest.items() if k != 'snapshot_digest'})
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='/Users/xu/git/memory-schema-rsi/g_v_test')
    parser.add_argument('--convs', required=True, help='逗号分隔，如 conv-26,conv-30')
    parser.add_argument('--output', default=str(DEFAULT_OUT))
    args = parser.parse_args()
    source = Path(args.source).resolve()
    out_root = Path(args.output).resolve()
    adapter = LocomoAdapter(ROOT / 'datasets/locomo/data/locomo10_zh.json')
    out_root.mkdir(parents=True, exist_ok=True)
    results = {}
    for conv in [c.strip() for c in args.convs.split(',') if c.strip()]:
        manifest = import_conv(source, out_root, conv, adapter)
        results[conv] = {k: manifest[k] for k in ('n_facts', 'n_nodes', 'n_vector_records', 'snapshot_digest')}
        print(json.dumps({'conv': conv, **results[conv]}, ensure_ascii=False))
    (out_root / 'index.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
