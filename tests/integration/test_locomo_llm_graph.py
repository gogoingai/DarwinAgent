"""Schema-driven snapshot construction with recorded model responses only."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from darwinagent.config import RunConfig
from darwinagent.contracts import CorpusBlock, SourceRef
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.steps import UnknownRequest
from darwinagent.schema.model import Schema
from tests.support.locomo import MINIMAL_S


class LLMGraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.block = CorpusBlock(
            SourceRef("message_text", "conv-test", "D1:1"), "小林买了一辆自行车。"
        )
        self.row = {
            "fid": "f1",
            "subject": "小林",
            "statement": self.block.text,
            "sources": ["D1:1"],
            "mentions": ["自行车"],
            "topics": [],
        }
        (self.snapshot / "facts.jsonl").write_text(json.dumps(self.row, ensure_ascii=False) + "\n")
        (self.snapshot / "vector").mkdir()
        (self.snapshot / "vector/index.jsonl").write_text('{"id":"f1"}\n')
        atomic_json(
            self.snapshot / "manifest.json",
            {
                "n_facts": 1,
                "facts_digest": digest([self.row]),
                "snapshot_digest": "test",
                "n_vector_records": 1,
                "memory_id_field": "编号",
            },
        )
        self.config = RunConfig(protocol_attempts=1, extraction_batch_chars=2000)
        self.addCleanup(mock.patch.stopall)
        mock.patch("datasets.locomo.llm_graph.attach_vector").start()

    def runtime(self, item=False, prompt="依据当前 S 构图"):
        raw = yaml.safe_load(MINIMAL_S)
        if item:
            raw["entity_types"]["物品"] = {
                "primary_key": ["名称"],
                "attributes": [{"name": "名称", "dtype": "string"}],
            }
            raw["relation_types"]["涉及物品"] = {"domain": "原子事实", "range": "物品"}
        return SimpleNamespace(
            schema=Schema.from_yaml(yaml.safe_dump(raw, allow_unicode=True)),
            prompt=lambda role: prompt,
        )

    def response(self):
        evidence = [{"fact_id": "f1", "source_id": self.block.source.id, "quote": "自行车"}]
        return {
            "entities": [
                {"type": "物品", "key": {"名称": "自行车"}, "properties": {}, "evidence": evidence}
            ],
            "relations": [
                {
                    "relation": "涉及物品",
                    "head": {"type": "原子事实", "key": {"编号": "f1"}},
                    "tail": {"type": "物品", "key": {"名称": "自行车"}},
                    "evidence": evidence,
                }
            ],
        }

    async def build(self, runtime, client):
        from datasets.locomo.llm_graph import LLMSnapshotGraphBuilder

        return await LLMSnapshotGraphBuilder(self.root / "cache").build(
            self.snapshot, runtime, (self.block,), client, self.config
        )

    async def test_schema_extension_materializes_new_type_and_relation(self):
        client = RecordedClient(
            {"extraction": [{"entities": [], "relations": []}, self.response()]}
        )
        before = await self.build(self.runtime(), client)
        after = await self.build(self.runtime(item=True), client)
        self.assertEqual(before.graph.number_of_nodes(), 1)
        self.assertEqual(after.graph.number_of_nodes(), 2)
        self.assertEqual([e["relation"] for *_, e in after.graph.edges(data=True)], ["涉及物品"])
        self.assertEqual(len(client.calls), 2)
        payload = json.loads(client.calls[-1]["messages"][1]["content"])
        self.assertIn("物品", payload["schema"])
        self.assertEqual(payload["facts"][0]["fid"], "f1")
        self.assertNotIn("questions", payload)
        self.assertNotIn("gold", payload)
        self.assertEqual(json.loads((self.snapshot / "facts.jsonl").read_text()), self.row)

    async def test_restart_reuses_graph_but_extract_prompt_change_rebuilds(self):
        first = RecordedClient({"extraction": [self.response()]})
        graph = await self.build(self.runtime(item=True), first)
        restarted = RecordedClient({})
        reused = await self.build(self.runtime(item=True), restarted)
        self.assertEqual(graph.graph.number_of_edges(), reused.graph.number_of_edges())
        self.assertEqual(restarted.calls, [])
        changed = RecordedClient({"extraction": [self.response()]})
        await self.build(self.runtime(item=True, prompt="核查实体分型后构图"), changed)
        self.assertEqual(len(changed.calls), 1)

    async def test_unsupported_quote_and_undeclared_type_are_rejected(self):
        bad = self.response()
        bad["entities"][0]["evidence"][0]["quote"] = "买了汽车"
        from darwinagent.agents.protocol import ProtocolError

        with self.assertRaises(ProtocolError) as failure:
            await self.build(self.runtime(item=True), RecordedClient({"extraction": [bad]}))
        self.assertIn("quote must be verbatim", str(failure.exception))
        self.assertIn("original_source_text", str(failure.exception))
        with self.assertRaises(ProtocolError):
            await self.build(self.runtime(), RecordedClient({"extraction": [self.response()]}))

    async def test_undefined_endpoint_and_atomic_fact_rewrite_are_rejected(self):
        from darwinagent.agents.protocol import ProtocolError

        bad = self.response()
        bad["relations"][0]["tail"]["key"]["名称"] = "不存在的物品"
        with self.assertRaises(ProtocolError):
            await self.build(self.runtime(item=True), RecordedClient({"extraction": [bad]}))
        bad = self.response()
        bad["entities"][0].update(
            type="原子事实", key={"编号": "f1"}, properties={"陈述": "伪造事实"}
        )
        with self.assertRaises(ProtocolError):
            await self.build(self.runtime(), RecordedClient({"extraction": [bad]}))

    async def test_unknown_request_is_not_silently_retried(self):
        client = RecordedClient({"extraction": [RuntimeError("connection interrupted")]})
        with self.assertRaises(UnknownRequest):
            await self.build(self.runtime(item=True), client)
        with self.assertRaises(UnknownRequest):
            await self.build(self.runtime(item=True), client)
        self.assertEqual(len(client.calls), 1)

    async def test_pipeline_failure_reports_active_graph_mode(self):
        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from darwinagent.kernel.registration import load_assets
        from tests.support.device import TASK, case

        class FailedLLMBuilder:
            def identity(self):
                return {"graph_mode": "llm", "graph_builder": "recorded_failure"}

            async def build(self, *args, **kwargs):
                raise ValueError("recorded graph failure")

        bundle = load_assets(TASK).export(self.root / "bundle")
        pipeline = Pipeline(
            RecordedClient({}),
            self.root / "run",
            frozen_snapshot=self.snapshot,
            graph_builder=FailedLLMBuilder(),
        )
        result = await pipeline.run(case(), TaskSpec.load(TASK / "task.yaml", bundle), self.config)
        failure = next(d for d in result.graph_diagnostics if d.get("status") == "execution_error")
        self.assertEqual(failure["graph_mode"], "llm")
        self.assertIn("recorded graph failure", failure["error"])

    async def test_pipeline_builds_checks_answers_and_resumes_with_same_graph(self):
        from collections import deque
        from dataclasses import replace

        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from darwinagent.kernel.assets import KernelAssets
        from darwinagent.kernel.registration import load_assets
        from datasets.locomo.llm_graph import LLMSnapshotGraphBuilder
        from tests.support.device import TASK, case
        from tests.support.device import client as recorded_client

        c = case()
        self.row.update(statement=c.corpus[0].text, sources=[c.corpus[0].source.location])
        (self.snapshot / "facts.jsonl").write_text(json.dumps(self.row, ensure_ascii=False) + "\n")
        assets = load_assets(TASK)
        schema_asset = next(a for a in assets.assets if a.kind == "S")
        raw = yaml.safe_load(schema_asset.content)
        raw["entity_types"]["原子事实"] = yaml.safe_load(MINIMAL_S)["entity_types"]["原子事实"]
        bundle = KernelAssets(
            tuple(
                replace(a, content=yaml.safe_dump(raw, allow_unicode=True)) if a.kind == "S" else a
                for a in assets.assets
            )
        ).export(self.root / "bundle")
        client = recorded_client(c)
        response = client.replies["extraction"].popleft()
        for entity in response["entities"]:
            entity["evidence"] = [
                {
                    "fact_id": "f1",
                    "source_id": entity.pop("source_id"),
                    "quote": entity.pop("quote"),
                }
            ]
        client.replies["extraction"] = deque([response])
        client.replies["answer"][0]["node_ids"] = ["n000001"]
        pipeline = Pipeline(
            client,
            self.root / "run",
            frozen_snapshot=self.snapshot,
            graph_builder=LLMSnapshotGraphBuilder(self.root / "graphs"),
        )
        mock.patch("darwinagent.experiments.snapshots.attach_vector").start()
        result = await pipeline.run(c, TaskSpec.load(TASK / "task.yaml", bundle), self.config)
        self.assertEqual(result.answers[0].status, "answered", result.to_dict())
        self.assertTrue(any(d.get("stage") == "graph_evidence" for d in result.graph_diagnostics))
        count = len(client.calls)
        await pipeline.run(c, TaskSpec.load(TASK / "task.yaml", bundle), self.config)
        self.assertEqual(len(client.calls), count)
