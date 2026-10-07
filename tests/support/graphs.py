"""Offline shared graphs fixtures; no test-case dependencies."""

import json
from pathlib import Path

from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from darwinagent.kernel.assets import Asset, KernelAssets
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


def build_snapshot(root, conv="conv-x"):
    """Import-path fixture: a gvtest-shaped source tree + adapter, run through the importer."""
    from darwinagent.kg.graph import save_graph
    from datasets.locomo.scripts.import_snapshots import import_conv

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
