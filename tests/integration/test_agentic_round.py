"""Agentic round (graph+vector) stage-one coverage: dates kernel, semantic_search lineage,
vector store round-trip, snapshot import + frozen-snapshot pipeline injection, vector_once
baseline isolation, cold-start S constraints and iteration scope gating. All offline."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.revision import AssetPatch, AssetRevisionService
from darwinagent.kernel.validation import atomic_memory_errors, validate_bundle
from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.dates import resolve_relative
from darwinagent.schema.model import Schema
from darwinagent.vector import LocalVectorStore

ROOT = Path(__file__).resolve().parents[2]


def fake_vector(text: str, dim: int = 64):
    """Deterministic offline embedding: character-bag vector, so near-identical texts stay
    near-identical under cosine (enough for retrieval-order tests, no network)."""
    vec = [0.0] * dim
    for ch in text:
        vec[ord(ch) % dim] += 1.0
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


class FakeEmbedder:
    model, base_url, dim = "fake", "fake", 64

    def embed(self, text):
        return fake_vector(text)


def gvtest_graph():
    """A miniature gvtest-style graph: 原子事实 with 出处 dia_ids + derived 人物/会话 nodes."""
    import networkx as nx

    g = nx.MultiDiGraph()
    g.add_node(
        '原子事实::[["编号","c-0001"]]',
        etype="原子事实",
        __key__='{"编号": "c-0001"}',
        __sources__=["conv-x"],
        编号="c-0001",
        陈述="甲计划下周修打印机",
        主体="甲",
        类型="计划",
        出处="D1:2",
    )
    g.add_node(
        '原子事实::[["编号","c-0002"]]',
        etype="原子事实",
        __key__='{"编号": "c-0002"}',
        __sources__=["conv-x"],
        编号="c-0002",
        陈述="乙觉得跑步能减压",
        主体="乙",
        类型="观点",
        出处="D1:3",
    )
    g.add_node(
        '人物::[["姓名","甲"]]',
        etype="人物",
        __key__='{"姓名": "甲"}',
        __sources__=["conv-x"],
        姓名="甲",
    )
    g.add_node(
        '人物::[["姓名","乙"]]',
        etype="人物",
        __key__='{"姓名": "乙"}',
        __sources__=["conv-x"],
        姓名="乙",
    )
    g.add_node(
        '会话::[["序号",1]]',
        etype="会话",
        __key__='{"序号": 1}',
        __sources__=["conv-x"],
        序号=1,
        日期="2024-05-01",
    )
    g.add_edge(
        '原子事实::[["编号","c-0001"]]', '人物::[["姓名","甲"]]', key="归属于", relation="归属于"
    )
    g.add_edge(
        '原子事实::[["编号","c-0002"]]', '人物::[["姓名","乙"]]', key="归属于", relation="归属于"
    )
    g.add_edge(
        '原子事实::[["编号","c-0001"]]', '会话::[["序号",1]]', key="记录于", relation="记录于"
    )
    g.add_edge(
        '原子事实::[["编号","c-0002"]]', '会话::[["序号",1]]', key="记录于", relation="记录于"
    )
    return g


def corpus():
    return (
        CorpusBlock(
            SourceRef("message_text", "conv-x", "D1:2"),
            "甲说：我下周修打印机。",
            {"speaker": "甲", "date": "2024-05-01"},
        ),
        CorpusBlock(
            SourceRef("message_text", "conv-x", "D1:3"),
            "乙说：跑步能减压。",
            {"speaker": "乙", "date": "2024-05-01"},
        ),
    )


def graph_result_with_vector():
    from darwinagent.contracts import GraphResult

    gr = GraphResult(gvtest_graph(), {b.source.id: b for b in corpus()})
    store = LocalVectorStore()
    store.upsert(
        [
            {
                "id": "c-0001",
                "text": "甲。甲计划下周修打印机",
                "meta": {"pool": "facts", "主体": "甲"},
                "vector": fake_vector("甲。甲计划下周修打印机"),
            },
            {
                "id": "c-0002",
                "text": "乙。乙觉得跑步能减压",
                "meta": {"pool": "facts", "主体": "乙"},
                "vector": fake_vector("乙。乙觉得跑步能减压"),
            },
        ]
    )
    from darwinagent.vector import VectorIndex

    object.__setattr__(gr, "vector", VectorIndex.attach(store, FakeEmbedder()))
    return gr


class DatesKernel(unittest.TestCase):
    def test_relative_resolution(self):
        from datetime import date

        anchor = date(2024, 5, 8)
        self.assertEqual(resolve_relative(anchor, "昨天")[0], "2024-05-07")
        self.assertEqual(resolve_relative(anchor, "去年"), ("2023", "年"))
        self.assertEqual(resolve_relative(anchor, "上周日")[0], "2024-05-05")
        resolved, _ = resolve_relative(anchor, "3个月前")
        self.assertTrue(resolved.startswith("2024-02"))
        self.assertEqual(resolve_relative(anchor, "说不清"), ("", "无"))

    def test_capability_wrapper(self):
        caps = DataCapabilities(graph_result_with_vector())
        out = caps.relative_date("2024-05-08", "上周日")
        self.assertEqual(out["resolved"], "2024-05-05")
        with self.assertRaises(ValueError):
            caps.relative_date("not-a-date", "昨天")


class SemanticSearchCapability(unittest.TestCase):
    def test_hits_resolve_to_rows_with_lineage(self):
        caps = DataCapabilities(graph_result_with_vector())
        rows = caps.semantic_search("甲。甲计划下周修打印机", limit=2)
        self.assertEqual(rows[0]["编号"], "c-0001")
        self.assertGreater(rows[0]["score"], 0.99)
        self.assertIn(rows[0]["node_id"], caps.read_ids)
        self.assertGreater(caps.read_operations, 0)

    def test_subject_filter_and_missing_index(self):
        caps = DataCapabilities(graph_result_with_vector())
        rows = caps.semantic_search("跑步", subject="甲", limit=5)
        self.assertEqual([r["编号"] for r in rows], ["c-0001"] if rows else [])
        from darwinagent.contracts import GraphResult

        bare = GraphResult(gvtest_graph(), {})
        with self.assertRaises(ValueError):
            DataCapabilities(bare).semantic_search("任意", limit=3)


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


SCHEMA_YAML = """meta:
  atomic_memory_type: 原子记忆
entity_types:
  原子记忆:
    description: 原子记忆单元
    primary_key: [编号]
    attributes:
      - {name: 编号, dtype: string}
      - {name: 陈述, dtype: string}
      - {name: 主体, dtype: string}
  人物:
    description: 主体
    primary_key: [姓名]
    attributes:
      - {name: 姓名, dtype: string}
relation_types: {}
axioms: []
"""


def cold_bundle(root, with_c=True):
    assets = [
        Asset(
            "schema",
            "S",
            SCHEMA_YAML,
            {"type": "any"},
            {"type": "any"},
            (),
            description="cold schema",
        ),
        Asset(
            "f_semantic",
            "F",
            "def run(params):\n return semantic_search(params['query'], subject=params.get('subject',''), limit=params.get('limit',8))",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            {"type": "array"},
            ["schema"],
            description="语义检索",
            trial_inputs=({"query": "打印机"},),
        ),
        *[
            Asset(f"p_{r}", "P", text, role=r, schema_dependencies=["schema"], description=r)
            for r, text in [
                ("extract", "抽取 ${schema}"),
                ("tools", "工具 ${schema} ${tools}"),
                ("answer", "作答 ${schema}"),
                ("review", "审查 ${schema}"),
            ]
        ],
    ]
    if with_c:
        assets.append(
            Asset(
                "c_answer",
                "C",
                'def check(candidate):\n return {"ok": True, "issues": []}',
                stage="answer",
                schema_dependencies=["schema"],
                description="check",
            )
        )
    return KernelAssets(tuple(assets), {"kind": "test"}).export(root / "assets")


class ColdStartConstraints(unittest.TestCase):
    def test_c_free_bundle_admits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = cold_bundle(root, with_c=False)
            self.assertTrue(any(a.kind == "F" for a in bundle.assets.assets))
            self.assertFalse(any(a.kind == "C" for a in bundle.assets.assets))

    def test_atomic_memory_floor(self):
        schema = Schema.from_yaml(SCHEMA_YAML)
        self.assertEqual(atomic_memory_errors(schema), [])
        broken = Schema.from_yaml(SCHEMA_YAML.replace("atomic_memory_type: 原子记忆", ""))
        self.assertTrue(atomic_memory_errors(broken))
        renamed = Schema.from_yaml(
            SCHEMA_YAML.replace("atomic_memory_type: 原子记忆", "atomic_memory_type: 不存在")
        )
        self.assertTrue(atomic_memory_errors(renamed))
        noattr = Schema.from_yaml(SCHEMA_YAML.replace("      - {name: 陈述, dtype: string}\n", ""))
        self.assertTrue(atomic_memory_errors(noattr))

    def test_revision_freezes_atomic_memory_and_scope(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = cold_bundle(root)
            svc = AssetRevisionService()
            a = base.get("p_answer")
            dropped = replace(a, content=a.content + " 更谨慎。")
            good = svc.propose(
                base,
                [AssetPatch(good_patch := dropped, a.fingerprint, "r", ("1:c::q1",))],
                root / "c1",
                ["1:c::q1"],
            )
            self.assertNotEqual(good.version, base.version)
            # S 换原子记忆类型名被拒
            s = base.get("schema")
            swapped = replace(
                s,
                content=s.content.replace(
                    "atomic_memory_type: 原子记忆", "atomic_memory_type: 人物"
                ),
            )
            with self.assertRaises(ValueError) as caught:
                svc.propose(
                    base,
                    [AssetPatch(swapped, s.fingerprint, "r", ("1:c::q1",))],
                    root / "c2",
                    ["1:c::q1"],
                )
            self.assertIn("atomic_memory_type", str(caught.exception))
            # scope 门控：只开 P 时 F 补丁被拒
            f = base.get("f_semantic")
            f_patch = AssetPatch(
                replace(
                    f, content="def run(params):\n return semantic_search(params['query'], limit=4)"
                ),
                f.fingerprint,
                "r",
                ("1:c::q1",),
            )
            with self.assertRaises(ValueError) as caught:
                svc.propose(base, [f_patch], root / "c3", ["1:c::q1"], allowed_kinds=("P",))
            self.assertIn("范围外", str(caught.exception))
            ok = svc.propose(
                base,
                [AssetPatch(dropped, a.fingerprint, "r", ("1:c::q1",))],
                root / "c4",
                ["1:c::q1"],
                allowed_kinds=("P",),
            )
            self.assertNotEqual(ok.version, base.version)


def build_snapshot(root, conv="conv-x"):
    """Import-path fixture: a gvtest-shaped source tree + adapter, run through the importer."""
    from datasets.locomo.scripts.import_snapshots import import_conv
    from darwinagent.kg.graph import save_graph

    src = root / "source"
    gdir = src / "runs" / conv / "graph_test01"
    gdir.mkdir(parents=True)
    save_graph(gvtest_graph(), gdir / "graph.json")
    (gdir / "facts.jsonl").write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False)
            for r in [
                {
                    "fid": "c-0001",
                    "subject": "甲",
                    "statement": "甲计划下周修打印机",
                    "sources": ["D1:2"],
                },
                {
                    "fid": "c-0002",
                    "subject": "乙",
                    "statement": "乙觉得跑步能减压",
                    "sources": ["D1:3"],
                },
            ]
        )
        + "\n"
    )
    vecdir = src / "data/vecstore"
    vecdir.mkdir(parents=True)
    store = LocalVectorStore()
    store.upsert(
        [
            {
                "id": "c-0001",
                "text": "甲。甲计划下周修打印机",
                "meta": {"pool": "facts", "主体": "甲"},
                "vector": fake_vector("甲。甲计划下周修打印机"),
            },
            {
                "id": "c-0002",
                "text": "乙。乙觉得跑步能减压",
                "meta": {"pool": "facts", "主体": "乙"},
                "vector": fake_vector("乙。乙觉得跑步能减压"),
            },
        ]
    )
    store.save(vecdir / f"facts_store_{conv}.jsonl")

    class Adapter:
        def generation_input(self, case_id):
            return CaseInput(
                case_id, corpus(), (QuestionInput("q1", "甲计划下周修打印机，做什么？"),)
            )

    out = root / "snapshots"
    manifest = import_conv(src, out, conv, Adapter())
    # 导入改写：事实节点来源到消息级，派生节点取并集
    return out / conv, manifest


class SnapshotPipeline(unittest.TestCase):
    def run_pipeline(self, root, config, mode):
        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from darwinagent.llm.recorded import RecordedClient
        from tests.fixtures import review

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


if __name__ == "__main__":
    unittest.main()
