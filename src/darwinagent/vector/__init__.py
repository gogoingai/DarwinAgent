"""Vector base capability: OpenAI-compatible /embeddings client, local JSONL+cosine store,
and the index handle that attaches a frozen store to a graph snapshot."""

from .embedder import Embedder, load_embedder
from .index import VectorIndex
from .store import LocalVectorStore, VecHit

__all__ = ["Embedder", "load_embedder", "LocalVectorStore", "VecHit", "VectorIndex"]
