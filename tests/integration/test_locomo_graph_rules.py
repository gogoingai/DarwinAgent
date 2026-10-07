"""固定事实→图投影（新模式底座，recheck4）离线合同测试：无模型、不触碰冻结面。

覆盖：投影确定性与行词表一致性（向量命中映射按 编号 回行）、S 绑定（未声明
投影词汇的 S 在 build_graph 被拒）、向量命中校验、缓存键不含题目/资产版本。
"""

import json
import unittest
from pathlib import Path

from datasets.locomo.graph_rules import (
    FACT_TYPE,
    PROJECTION_VERSION,
    graph_cache_key,
    load_facts,
    project_candidates,
    rebuild_graph,
    vector_hit_check,
)

SNAPSHOTS = Path("tests/fixtures/locomo_snapshot")

MINIMAL_S = """\
meta:
  task: conversation_memory
entity_types:
  原子事实:
    primary_key: [编号]
    attributes:
      - {name: 编号, dtype: string}
      - {name: 陈述, dtype: string}
      - {name: 主体, dtype: string}
      - {name: 类型, dtype: string}
      - {name: 日期, dtype: string}
      - {name: 日期粒度, dtype: string}
      - {name: 日期原文, dtype: string}
      - {name: 数值, dtype: string}
      - {name: 主题, dtype: string}
      - {name: 出处, dtype: string}
  人物:
    primary_key: [姓名]
    attributes:
      - {name: 姓名, dtype: string}
  主题:
    primary_key: [名称]
    attributes:
      - {name: 名称, dtype: string}
  会话:
    primary_key: [序号]
    attributes:
      - {name: 序号, dtype: int}
      - {name: 日期, dtype: string}
      - {name: 星期, dtype: string}
relation_types:
  归属于: {domain: 原子事实, range: 人物}
  属于主题: {domain: 原子事实, range: 主题}
  记录于: {domain: 原子事实, range: 会话}
"""


class FactProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.facts, cls.manifest = load_facts(SNAPSHOTS / "conv-26")
        cls.schema = __import__("darwinagent.schema.model", fromlist=["Schema"]).Schema.from_yaml(
            MINIMAL_S
        )

    def test_projection_is_deterministic_and_traceable(self):
        e1, r1 = project_candidates(self.facts)
        e2, r2 = project_candidates(self.facts)
        self.assertEqual(
            [(e.etype, e.key, e.properties, e.chunk_id) for e in e1],
            [(e.etype, e.key, e.properties, e.chunk_id) for e in e2],
        )
        self.assertEqual(r1, r2)
        # 每个事实行节点都在，且来源 chunk 指回事实 ID
        fact_nodes = [e for e in e1 if e.etype == FACT_TYPE]
        self.assertEqual(len(fact_nodes), len(self.facts))
        # 无 corpus 时 chunk＝追溯标记（fact:<fid>）；无来源派生事实为空 chunk
        self.assertTrue(all(e.chunk_id.startswith("fact:") or e.chunk_id == "" for e in fact_nodes))
        # 行词表与冻结图一致（向量命中映射按 编号 回行）
        sample = fact_nodes[0]
        self.assertIn("编号", sample.key)
        for field in ("陈述", "主体", "类型", "日期", "日期原文", "主题", "出处"):
            self.assertIn(field, sample.properties)

    def test_rebuild_graph_and_vector_hits(self):
        g = rebuild_graph(self.facts, self.schema)
        self.assertEqual(g.graph.get("projection_version"), PROJECTION_VERSION)
        check = vector_hit_check(g, SNAPSHOTS / "conv-26")
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["graph_fact_rows"], len(self.facts))
        self.assertEqual(check["vector_records"], self.manifest.get("n_vector_records"))

    def test_snapshot_graph_builder_returns_ready_graph_result(self):
        """Pipeline graph_builder 契约：rebuild_snapshot_graph 返回挂好向量的
        GraphResult（命中校验在内）、诊断带 snapshot_digest（准入报告身份绑定）。"""
        from datasets.locomo.graph_rules import rebuild_snapshot_graph

        graph = rebuild_snapshot_graph(
            SNAPSHOTS / "conv-26", self.schema, embedder_factory=lambda: None
        )
        self.assertTrue(graph.graph.number_of_nodes() > 0)
        self.assertIsNotNone(getattr(graph, "vector", None))
        diag = next(d for d in graph.diagnostics if isinstance(d, dict))
        self.assertEqual(diag.get("snapshot_digest"), self.manifest["snapshot_digest"])
        self.assertTrue(vector_hit_check(graph, SNAPSHOTS / "conv-26")["ok"])

    def test_corpus_sources_resolve_to_real_blocks(self):
        """出处映射：corpus 给定时，事实行节点 __sources__ 是真实语料块 id，
        `graph.sources[s]` 逐个可解析（答题/审查证据解析契约）；派生节点来源＝
        关联事实来源并集；未登记出处＝拒绝。"""
        from datasets.locomo.adapter import LocomoAdapter
        from datasets.locomo.graph_rules import project_candidates, rebuild_graph
        from pathlib import Path as _P

        case = LocomoAdapter(_P("datasets/locomo/data/locomo10_zh.json")).generation_input(
            "conv-26"
        )
        corpus_map = {b.source.id: b for b in case.corpus}
        g = rebuild_graph(self.facts, self.schema, case.corpus)
        fact_nodes = [nd for _, nd in g.nodes(data=True) if nd.get("etype") == FACT_TYPE]
        self.assertEqual(len(fact_nodes), len(self.facts))
        sourced = 0
        for nd in fact_nodes:
            for s in nd["__sources__"]:
                self.assertIn(s, corpus_map, f"来源 {s} 未登记")
            sourced += bool(nd["__sources__"])
        # 无来源派生事实（manifest n_unsourced_facts）如实为空，不虚构
        self.assertEqual(len(self.facts) - sourced, self.manifest.get("n_unsourced_facts"))
        person = next(nd for _, nd in g.nodes(data=True) if nd.get("etype") == "人物")
        self.assertTrue(all(s in corpus_map for s in person["__sources__"]))
        # 离线无 corpus 时退回追溯标记（不参与证据解析）
        entities, _ = project_candidates(self.facts[:3])
        self.assertTrue(
            all(e.chunk_id.startswith("fact:") for e in entities if e.etype == FACT_TYPE)
        )

    def test_undeclared_schema_is_rejected(self):
        """S 绑定：S 未声明投影硬前提（缺 原子事实）→ 重建被拒，不是静默降级。"""
        from darwinagent.schema.model import Schema

        poor = Schema.from_yaml(
            "meta:\n  task: conversation_memory\n"
            "entity_types:\n"
            "  人物:\n    primary_key: [姓名]\n"
            "    attributes:\n      - {name: 姓名, dtype: string}\n"
            "relation_types:\n  归属于: {domain: 原子事实, range: 人物}\n"
        )
        with self.assertRaises(ValueError):
            rebuild_graph(self.facts, poor)

    def test_schema_shapes_the_graph(self):
        """S 即构图规则：S 少声明（无 主题/会话）→ 图真实变小（少类型/边/属性）；
        硬前提违反（原子事实主键非 编号）→ 拒绝。"""
        from darwinagent.schema.model import Schema

        smaller = Schema.from_yaml(
            MINIMAL_S.replace(
                "  主题:\n"
                "    primary_key: [名称]\n"
                "    attributes:\n"
                "      - {name: 名称, dtype: string}\n",
                "",
            )
            .replace(
                "  会话:\n"
                "    primary_key: [序号]\n"
                "    attributes:\n"
                "      - {name: 序号, dtype: int}\n"
                "      - {name: 日期, dtype: string}\n"
                "      - {name: 星期, dtype: string}\n",
                "",
            )
            .replace("  属于主题: {domain: 原子事实, range: 主题}\n", "")
            .replace("  记录于: {domain: 原子事实, range: 会话}\n", "")
        )
        full = rebuild_graph(self.facts, self.schema)
        slim = rebuild_graph(self.facts, smaller)
        types_full = {d.get("etype") for _, d in full.nodes(data=True)}
        types_slim = {d.get("etype") for _, d in slim.nodes(data=True)}
        self.assertEqual(types_full, {"原子事实", "人物", "主题", "会话"})
        self.assertEqual(types_slim, {"原子事实", "人物"})
        # 事实行数不变（向量映射面稳定），边与属性面随 S 变
        facts_full = sum(1 for _, d in full.nodes(data=True) if d.get("etype") == FACT_TYPE)
        facts_slim = sum(1 for _, d in slim.nodes(data=True) if d.get("etype") == FACT_TYPE)
        self.assertEqual(facts_full, facts_slim)
        self.assertGreater(full.number_of_edges(), slim.number_of_edges())

    def test_wrong_fact_primary_key_is_rejected(self):
        from darwinagent.schema.model import Schema

        bad = MINIMAL_S.replace("    primary_key: [编号]\n", "    primary_key: [陈述]\n", 1)
        bad = bad.replace("      - {name: 编号, dtype: string}\n", "", 1)
        with self.assertRaises(ValueError):
            rebuild_graph(self.facts, Schema.from_yaml(bad))

    def test_cache_key_binds_facts_schema_projection_only(self):
        from darwinagent.runtime.artifacts import digest

        k1 = graph_cache_key(self.manifest["facts_digest"], "schema-a")
        k2 = graph_cache_key(self.manifest["facts_digest"], "schema-a")
        k3 = graph_cache_key(self.manifest["facts_digest"], "schema-b")
        k4 = graph_cache_key(digest({"other": 1}), "schema-a")
        self.assertEqual(k1, k2)  # 同事实＋同 S → 同图缓存键
        self.assertNotEqual(k1, k3)  # S 变 → 重建
        self.assertNotEqual(k1, k4)  # 事实变（此模式下不该发生）→ 键变
        # 键不含题目列表/资产版本：纯函数输入决定
        self.assertNotIn("train", k1)


if __name__ == "__main__":
    unittest.main()
