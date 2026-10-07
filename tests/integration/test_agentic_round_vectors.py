"""Offline regression scenarios for vectors."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.vector import LocalVectorStore
from tests.support.graphs import fake_vector


class VectorStoreRoundTrip(unittest.TestCase):
    def test_save_load_search(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "index.jsonl"
            store = LocalVectorStore()
            store.upsert(
                [
                    {
                        "id": "a",
                        "text": "ta",
                        "meta": {"pool": "facts"},
                        "vector": fake_vector("ta"),
                    },
                    {
                        "id": "b",
                        "text": "tb",
                        "meta": {"pool": "facts"},
                        "vector": fake_vector("tb"),
                    },
                ]
            )
            store.save(path)
            back = LocalVectorStore.load(path)
            self.assertEqual(len(back), 2)
            hits = back.search(fake_vector("ta"), top_k=1)
            self.assertEqual(hits[0].id, "a")
