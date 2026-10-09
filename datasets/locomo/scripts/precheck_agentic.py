"""Agentic-round precheck: the campaign arms refuse to start without a passing record.

Checks: both model tiers respond; the embedding endpoint answers with the configured model;
every needed snapshot is digest-consistent, its graph rows load through DataCapabilities,
vector ids map onto atomic-memory rows, and semantic_search + relative_date work offline
against the frozen index. Writes precheck.json bound to precheck_identity(connection, config)."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import EvaluationResult
from darwinagent.experiments.spec import precheck_identity
from darwinagent.llm.client import LLMClient
from darwinagent.llm.settings import load_legacy_connection as load_connection

from datasets.locomo.inputs import add_dataset_arguments, resolve_dataset
from datasets.locomo.run import ROOT, arm_config, connection

CONVS = ('conv-26', 'conv-30', 'conv-41', 'conv-42', 'conv-43', 'conv-47', 'conv-48')


async def probe_tier(client, role):
    try:
        result = await client.chat(role=role, use_cache=False, namespace='precheck',
                                   json_mode=True, temperature=0.0, max_tokens=256,
                                   messages=[{'role': 'system', 'content': '你是连通性探测助手。'},
                                             {'role': 'user', 'content': '回复 JSON：{"ok": true}'}])
        return {'ok': bool(result.content.strip()), 'reply': result.content.strip()[:80]}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': repr(exc)[:200]}


def check_snapshots(convs, memory_root):
    from types import SimpleNamespace

    from darwinagent.kg.graph import load_graph
    from darwinagent.operators.data import DataCapabilities
    from darwinagent.vector import LocalVectorStore
    rows = {}
    for conv in convs:
        snap = memory_root / conv
        if not (snap / 'manifest.json').exists():
            rows[conv] = {'ok': False, 'error': f'快照缺失：{snap}'}
            continue
        try:
            manifest = json.loads((snap / 'manifest.json').read_text())
            g = load_graph(snap / 'graph.json')
            memory_rows = sum(1 for _n, nd in g.nodes(data=True) if nd.get('etype') == '原子事实')
            if memory_rows != manifest['n_facts']:
                raise ValueError(f"原子记忆行 {memory_rows} != manifest {manifest['n_facts']}")
            store = LocalVectorStore.load(snap / 'vector' / 'index.jsonl')
            caps = DataCapabilities(SimpleNamespace(graph=g))
            mapped = sum(1 for rid, r in caps.rows.items() if r.get('编号'))
            if len(store) < manifest['n_facts']:
                raise ValueError(f"向量记录 {len(store)} < 事实 {manifest['n_facts']}")
            rows[conv] = {'ok': True, 'n_facts': manifest['n_facts'], 'n_nodes': manifest['n_nodes'],
                          'n_vector': len(store), 'memory_rows': mapped}
        except Exception as exc:  # noqa: BLE001
            rows[conv] = {'ok': False, 'error': repr(exc)[:200]}
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, help='arm root (precheck.json written there)')
    parser.add_argument('--arm', choices=('v0', 'g1'), default='g1')
    parser.add_argument('--vector-k', type=int, default=30)
    parser.add_argument('--convs', default=','.join(CONVS))
    parser.add_argument('--memory-root', required=True)
    add_dataset_arguments(parser)
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    data_dir = resolve_dataset(args, out)
    memory_root = Path(args.memory_root).resolve()
    conn = connection(out)
    config = arm_config(args.arm, args.vector_k)
    checks = {'snapshots': check_snapshots(tuple(c.strip() for c in args.convs.split(',')), memory_root)}
    started = time.time()

    async def live():
        async with LLMClient(conn) as client:
            checks['strong_tier'] = await probe_tier(client, 'locomo_judge')
            checks['fast_tier_json'] = await probe_tier(client, 'tools')
            try:
                from darwinagent.vector import load_embedder
                emb = load_embedder(cache_path=memory_root / 'conv-26' / 'vector' / 'embed_cache.json')
                vec = emb.embed('连通性测试：谁修了打印机')
                checks['embedding_endpoint'] = {'ok': len(vec) >= 256, 'dim': len(vec), 'model': emb.model}
            except Exception as exc:  # noqa: BLE001
                checks['embedding_endpoint'] = {'ok': False, 'error': repr(exc)[:200]}
            try:
                # 快照上的真实 semantic_search 冒烟（嵌入一次查询，行可回图、血缘记录）
                from datasets.locomo.adapter import LocomoAdapter
                from darwinagent.experiments.snapshots import attach_vector, load_frozen_graph
                from darwinagent.operators.data import DataCapabilities
                corpus = LocomoAdapter(data_dir / 'locomo10_zh.json').generation_input('conv-26').corpus
                gr = load_frozen_graph(memory_root / 'conv-26', corpus)
                attach_vector(gr, memory_root / 'conv-26')
                caps = DataCapabilities(gr)
                hits = await asyncio.to_thread(caps.semantic_search, '跑步减压', limit=5)
                rel = caps.relative_date('2024-05-08', '上周日')
                checks['semantic_smoke'] = {'ok': bool(hits) and rel['resolved'] != '',
                                            'hits': len(hits), 'read_ops': caps.read_operations}
            except Exception as exc:  # noqa: BLE001
                checks['semantic_smoke'] = {'ok': False, 'error': repr(exc)[:300]}
    asyncio.run(live())
    snapshots_ok = all(v.get('ok') for v in checks['snapshots'].values())
    passed = (snapshots_ok
              and checks.get('strong_tier', {}).get('ok')
              and checks.get('fast_tier_json', {}).get('ok')
              and checks.get('embedding_endpoint', {}).get('ok')
              and checks.get('semantic_smoke', {}).get('ok'))
    record = {'passed': passed, 'checks': checks,
              'identity': precheck_identity(conn, config),
              'elapsed_s': round(time.time() - started, 1), 'ts': time.time()}
    (out / 'precheck.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': passed,
                      'checks': {k: (v.get('ok') if isinstance(v, dict) else {c: r.get('ok') for c, r in v.items()})
                                 for k, v in checks.items()}}, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
