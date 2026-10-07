"""Frozen vector index attached to a graph snapshot.

The index resolves semantic hits back to graph rows by the atomic-memory id attribute
(default 编号), so read lineage and evidence attribution stay row-level. It is data, not an
asset: every arm of an experiment consumes the identical frozen store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class VectorIndex:
    search_vectors: Callable[[str, int], list]  # (query, top_k) -> [(id, score, meta)]
    id_field: str = "编号"

    @classmethod
    def attach(cls, store, embedder, id_field="编号"):
        def search_vectors(query: str, top_k: int):
            hits = store.search(embedder.embed(query), top_k=top_k, pool="facts")
            return [(h.id, h.score, h.meta) for h in hits]

        return cls(search_vectors=search_vectors, id_field=id_field)

    def search(self, query: str, top_k: int = 30):
        return self.search_vectors(query, top_k)
