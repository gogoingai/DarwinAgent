"""Frozen memory snapshots shared by every arm of an experiment.

A snapshot directory holds one conversation's frozen memory plane, built ONCE by an importer
(e.g. datasets/locomo/scripts/import_snapshots.py from the g_v_test pinned builds):
    graph.json        the frozen graph (sources already rewritten to message-level refs)
    facts.jsonl       the atomic memory records
    vector/           index.jsonl (+ meta.json, embed_cache.json)
    manifest.json     digests + provenance

Integrity is by digest, the same discipline as the corpus: a snapshot never re-derives, so
arms differ only in assets. Validation at load time is fingerprint-based; admission-time
quality gates (F trials against the real graph, counterexample probes, task graph C) run in
the pipeline, not here."""

from __future__ import annotations

import json
from pathlib import Path


def snapshot_manifest(snapshot_dir) -> dict:
    path = Path(snapshot_dir) / "manifest.json"
    if not path.exists():
        raise ValueError(f"快照缺 manifest：{path}")
    return json.loads(path.read_text())


def snapshot_digest(snapshot_dir) -> str:
    manifest = snapshot_manifest(snapshot_dir)
    for key in ("graph_digest", "facts_digest", "vector_digest"):
        if not manifest.get(key):
            raise ValueError(f"快照 manifest 缺 {key}：{snapshot_dir}")
    return manifest["snapshot_digest"]


def attach_vector(graph_result, snapshot_dir, embedder_factory=None):
    """Attach the frozen vector index to a GraphResult (in place) for semantic_search."""
    from darwinagent.vector import LocalVectorStore, VectorIndex

    snapshot_dir = Path(snapshot_dir)
    manifest = snapshot_manifest(snapshot_dir)
    store = LocalVectorStore.load(snapshot_dir / "vector" / "index.jsonl")
    if len(store) != manifest.get("n_vector_records", len(store)):
        raise ValueError("向量索引与快照 manifest 记录数不符")
    if not len(store):
        # Empty prepared snapshots have no embedding space to query. Keep the
        # search interface available without constructing a model connection.
        object.__setattr__(
            graph_result,
            "vector",
            VectorIndex(lambda query, top_k: [], manifest.get("memory_id_field", "编号")),
        )
        return graph_result
    if embedder_factory is None:

        def embedder_factory():
            from darwinagent.vector import load_embedder

            return load_embedder(cache_path=snapshot_dir / "vector" / "embed_cache.json")

    # GraphResult is frozen; the vector handle is attached through the dataclass back door.
    object.__setattr__(
        graph_result,
        "vector",
        VectorIndex.attach(
            store, embedder_factory(), id_field=manifest.get("memory_id_field", "编号")
        ),
    )
    return graph_result


def load_frozen_graph(snapshot_dir, corpus):
    """GraphResult from a frozen snapshot: graph bytes are digest-verified; sources come from
    the live case corpus (same adapter, same fingerprint domain); the vector index attaches
    on the instance. The graph is frozen input data — schema re-typing does not run here."""
    from types import MappingProxyType

    import networkx as nx

    from darwinagent.contracts import GraphResult
    from darwinagent.kg.graph import load_graph
    from darwinagent.runtime.artifacts import digest

    snapshot_dir = Path(snapshot_dir)
    manifest = snapshot_manifest(snapshot_dir)
    payload = json.loads((snapshot_dir / "graph.json").read_text())
    if digest(payload) != manifest["graph_digest"]:
        raise ValueError("快照图指纹不符（graph.json 与 manifest 不一致）")
    graph = GraphResult(
        nx.freeze(load_graph(snapshot_dir / "graph.json")),
        MappingProxyType({b.source.id: b for b in corpus}),
        diagnostics=(manifest,),
        vector=None,
    )
    known_sources = set(graph.sources)
    for _nid, nd in graph.graph.nodes(data=True):
        # 空来源（无来源派生事实）允许并在 manifest 披露；未知来源一律拒绝。
        if set(nd.get("__sources__", [])) - known_sources:
            raise ValueError("快照图节点含未登记来源（导入改写不完整）")
    return graph
