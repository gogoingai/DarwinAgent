"""Offline regression scenarios for snapshots."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, QuestionInput
from tests.support.graphs import (
    ROOT,
    FakeEmbedder,
    build_snapshot,
    cold_bundle,
    corpus,
)


class SnapshotPipeline(unittest.TestCase):
    def run_pipeline(self, root, config, mode):
        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from darwinagent.llm.recorded import RecordedClient
        from tests.support.device import review

        snapshot, manifest = build_snapshot(root)
        bundle = cold_bundle(root)
        spec = TaskSpec.load(ROOT / "tasks/conversation_memory/task.yaml").with_bundle(bundle)
        case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "甲计划下周修打印机，做什么？"),))
        replies = {
            "tools": [
                {
                    "action": "call",
                    "asset_id": "f_semantic",
                    "parameters": {"query": "甲计划下周修打印机"},
                },
                {"action": "ready"},
            ],
            "answer": [
                {"status": "answered", "answer": "甲计划下周修打印机。", "node_ids": ["n000000"]}
            ],
            "review": [review()],
        }
        client = RecordedClient(replies)
        result = asyncio.run(
            Pipeline(
                client,
                root / "generation",
                frozen_snapshot=snapshot,
                embedder_factory=lambda: FakeEmbedder(),
            ).run(case, spec, config)
        )
        return result, manifest, client

    def test_frozen_snapshot_injection_and_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result, manifest, _ = self.run_pipeline(root, RunConfig(protocol_attempts=1), "agentic")
            self.assertEqual(result.answers[0].status, "answered")
            self.assertEqual(result.memory_count, manifest["n_facts"])
            self.assertEqual(result.memory_fingerprint, manifest["facts_digest"])
            # 零抽取调用：RecordedClient 只收到 tools/answer/review
            stored = json.loads((root / "generation" / "conv-x" / "result.json").read_text())
            self.assertEqual(stored["memory_count"], 2)

    def test_vector_once_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = RunConfig(protocol_attempts=1, retrieval_mode="vector_once", vector_k=1)
            result, manifest, _ = self.run_pipeline(root, config, "vector_once")
            self.assertEqual(result.answers[0].status, "answered")
            trace = result.answers[0].trace
            retrieval = [t for t in trace if t.get("stage") == "retrieval"]
            self.assertEqual(len(retrieval), 1)
            self.assertEqual(retrieval[0]["mode"], "vector_once")
            self.assertEqual(retrieval[0]["k"], 1)

    def test_snapshot_digest_guards_identity(self):
        from darwinagent.experiments.snapshots import load_frozen_graph

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, manifest = build_snapshot(root)
            tampered = dict(manifest)
            tampered["graph_digest"] = "0" * 64
            (snapshot / "manifest.json").write_text(json.dumps(tampered))
            with self.assertRaises(ValueError):
                load_frozen_graph(snapshot, corpus())
