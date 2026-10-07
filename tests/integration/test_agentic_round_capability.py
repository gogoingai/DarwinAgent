"""Offline regression scenarios for capability."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.kernel.revision import AssetPatch, AssetRevisionService
from darwinagent.kernel.validation import atomic_memory_errors
from darwinagent.operators.data import DataCapabilities
from darwinagent.schema.model import Schema
from tests.support.graphs import SCHEMA_YAML, cold_bundle, graph_result_with_vector, gvtest_graph


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
                [AssetPatch(_good_patch := dropped, a.fingerprint, "r", ("1:c::q1",))],
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
