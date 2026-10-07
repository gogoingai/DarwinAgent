"""Offline regression scenarios for capability."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    QuestionInput,
)
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.validation import capability_floor_errors, capability_names


class RetrievalFloorTests(unittest.TestCase):
    def F(self, fid, src):
        return Asset(fid, "F", src, {"type": "any"}, {"type": "any"}, trial_inputs=({"q": "x"},))

    def test_capability_names_mapping(self):
        self.assertEqual(
            capability_names({"semantic_search": True, "traversal": True}),
            ("semantic_search", "traverse"),
        )
        self.assertEqual(capability_names({"semantic_search": False}), ())
        self.assertEqual(capability_names({}), ())

    class _Assets:  # 能力底线只看 F 集，不需要完整 bundle
        def __init__(self, *fs):
            self.assets = fs

    def test_comment_only_mentions_rejected(self):
        # 评审#4 反例：注释里写着 semantic_search/traverse、实际只执行 nodes——必须拒绝
        fake = self._Assets(
            self.F(
                "fake",
                'def run(p):\n # semantic_search and traverse are great\n s = "semantic_search traverse"\n return nodes({})',
            )
        )
        missing = capability_floor_errors(fake, ("semantic_search", "traverse"))
        self.assertEqual(len(missing), 2)

    def test_real_calls_pass(self):
        real = self._Assets(
            self.F("v", 'def run(p):\n return semantic_search(p["q"])'),
            self.F("t", 'def run(p):\n return traverse(p["id"])'),
        )
        self.assertEqual(capability_floor_errors(real, ("semantic_search", "traverse")), [])

    def test_trial_floor_requires_positive_execution(self):
        from darwinagent.kernel.validation import trial_capability_floor_errors

        untouched = ({"asset_id": "a", "data": [], "capability_calls": {}},)
        self.assertEqual(
            len(trial_capability_floor_errors(untouched, ("semantic_search", "traverse"))), 2
        )
        empty_but_executed = (
            {"asset_id": "a", "data": [], "capability_calls": {"semantic_search": 1}},
        )
        problems = trial_capability_floor_errors(
            empty_but_executed, ("semantic_search", "traverse")
        )
        self.assertEqual(len(problems), 1)  # 空结果如实记录：执行过即算触发
        self.assertEqual(
            trial_capability_floor_errors(
                ({"capability_calls": {"semantic_search": 2, "traverse": 1}},),
                ("semantic_search", "traverse"),
            ),
            [],
        )

    def test_floor_requires_both_classes(self):
        keyword_only = self._Assets(self.F("search", 'def run(p):\n return search(p["q"])'))
        missing = capability_floor_errors(keyword_only, ("semantic_search", "traverse"))
        self.assertEqual(len(missing), 2)  # 关键词 search 不满足任何一类
        with_vec = self._Assets(
            self.F("v", 'def run(p):\n return semantic_search(p["q"])'),
            self.F("t", 'def run(p):\n return traverse(p["id"])'),
        )
        self.assertEqual(capability_floor_errors(with_vec, ("semantic_search", "traverse")), [])


class EmbedderCacheConcurrencyTests(unittest.TestCase):
    def test_concurrent_flush_never_loses_tmp(self):
        # conv-47 事故回归：共享固定 .tmp 名在并发缓存未命中时互相抢文件 →
        # FileNotFoundError 记为整题故障。唯一临时名后并发 flush 必须全部成功。
        import threading

        from darwinagent.vector.embedder import Embedder

        emb = Embedder.__new__(Embedder)
        with tempfile.TemporaryDirectory() as td:
            emb.cache_path = Path(td) / "embed_cache.json"
            emb._cache = {f"q{i}": [0.1] * 8 for i in range(100)}
            errors = []
            barrier = threading.Barrier(8)

            def flush_many():
                try:
                    barrier.wait()
                    for _ in range(50):
                        emb._flush()
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=flush_many) for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            self.assertTrue(emb.cache_path.exists())
            import json as _json

            self.assertEqual(len(_json.loads(emb.cache_path.read_text())), 100)
            self.assertEqual(list(Path(td).glob("*.tmp")), [])  # 无残留临时文件


class PreflightCapabilityTests(unittest.TestCase):
    @staticmethod
    def _preflight_sync(runner, *args, **kwargs):
        return asyncio.run(runner._preflight(*args, **kwargs))

    def _runner_with_graph(self, root):
        from darwinagent.experiments.runner import ExperimentRunner
        from darwinagent.experiments.snapshots import load_frozen_graph
        from tests.support.graphs import (
            FakeEmbedder,
            build_snapshot,
            cold_bundle,
            corpus,
        )

        snapshot, _ = build_snapshot(root)
        from darwinagent.experiments.snapshots import attach_vector

        graph = load_frozen_graph(snapshot, corpus())
        attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())

        class C:
            id = "c"
            questions = (QuestionInput("q1", "?"), QuestionInput("q2", "?"))

        runner = ExperimentRunner(
            type("Adapter", (), {"generation_input": staticmethod(lambda i: C())})(),
            lambda c, p: None,
            None,
            RunConfig(function_timeout_s=15.0),
            None,
            root / "r",
            client_factory=lambda s: None,
            bootstrap_trial_graph=graph,
        )
        return runner, cold_bundle(root / "b")

    def test_untriggered_capability_rejected_before_stage(self):
        # 评审③反例：semantic_search 在未执行分支里（AST 有调用），试跑只执行 nodes
        # ——候选预检必须拒绝
        from darwinagent.kernel import TaskSpec
        from tests.support.graphs import ROOT

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner, bundle = self._runner_with_graph(root)
            spec = TaskSpec.load(ROOT / "tasks/conversation_memory/task.yaml")
            from darwinagent.kernel.assets import Asset

            fake = Asset(
                "f_semantic",
                "F",
                "def run(params):\n if params.get('mode')=='vec':\n  return semantic_search(params['query'], limit=4)\n return nodes('原子事实', limit=2)\n",
                {"type": "object", "properties": {"mode": {"type": "string"}}},
                {"type": "array"},
                trial_inputs=({"mode": "plain"},),
                schema_dependencies=["schema"],
                description="fake",
            )
            traverse = Asset(
                "f_traverse",
                "F",
                "def run(params):\n return traverse(params['node_id'], params['relation'])\n",
                {
                    "type": "object",
                    "properties": {"node_id": {"type": "string"}, "relation": {"type": "string"}},
                },
                {"type": "array"},
                trial_inputs=({"node_id": "n000000", "relation": "归属于"},),
                schema_dependencies=["schema"],
                description="关系遍历",
            )
            others = [a for a in bundle.assets.assets if a.id != "f_semantic"] + [traverse]

            fake_bundle = self._export(root, tuple(others + [fake]))
            with self.assertRaises(ValueError) as caught:
                self._preflight_sync(runner, fake_bundle, spec)
            self.assertIn("候选能力试跑不合格", str(caught.exception))
            # 合格候选（真实触发 semantic_search）通过
            good = self._export(root, tuple(bundle.assets.assets) + (traverse,))
            self._preflight_sync(runner, good, spec)

    def _export(self, root, assets):
        import tempfile as _tf

        return KernelAssets(assets).export(Path(_tf.mkdtemp(prefix="pf-")) / "exported")


class FUnitTestsTests(unittest.TestCase):
    """用户拍板：冒烟之外必须有单测——准入试跑并入真实数据形态压力矩阵
    （空行/图头尾/列表字段行）。容器 str() 类分支错误在准入层暴露（R7 事故：14 题）。"""

    def test_stress_samples_expose_unsupported_container_conversion(self):
        """Current sandbox rejects container string conversion; stress trials must expose it."""
        import tempfile

        from darwinagent.experiments.snapshots import load_frozen_graph
        from darwinagent.experiments.trials import stress_trial_samples
        from darwinagent.kernel.assets import Asset
        from darwinagent.kernel.functions import FunctionRegistry
        from darwinagent.operators.sandbox import Limits
        from tests.support.graphs import (
            build_snapshot,
            cold_bundle,
            corpus,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            # 坏 F：对输入 rows 的字段直接 str()（R7 十四连故障同款）
            bad = Asset(
                "f_bad",
                "F",
                "def run(params):\n out=[]\n for r in params['rows']:\n  out.append(str(r.get('source_ids')))\n return {'rows': out}\n",
                {
                    "type": "object",
                    "properties": {"rows": {"type": "array"}},
                    "additionalProperties": True,
                },
                {
                    "type": "object",
                    "properties": {"rows": {"type": "array"}},
                    "additionalProperties": True,
                },
                ["schema"],
                description="bad",
                trial_inputs=({"rows": [{"node_id": "n000000"}]},),
            )
            bundle = KernelAssets(
                tuple([a for a in cold_bundle(root).assets.assets if a.kind != "F"] + [bad])
            ).export(root / "b")
            reg = FunctionRegistry(bundle, Limits(30000, 15.0, 180000))
            samples = stress_trial_samples([{"rows": [{"node_id": "n000000"}]}], graph)
            self.assertTrue(samples, "压力样本应非空")
            from darwinagent.operators.sandbox import SandboxError

            failures = []
            for sample in samples:
                try:
                    reg.call("f_bad", sample, graph)
                except SandboxError as exc:
                    failures.append(str(exc))
            self.assertTrue(
                any("Container-to-string conversion is unsupported" in e for e in failures)
            )

    def test_stress_samples_shapes(self):
        import tempfile

        from darwinagent.experiments.trials import stress_trial_samples
        from tests.support.graphs import build_snapshot

        with tempfile.TemporaryDirectory() as td:
            snapshot, _ = build_snapshot(Path(td))
            from darwinagent.experiments.snapshots import load_frozen_graph
            from tests.support.graphs import corpus

            graph = load_frozen_graph(snapshot, corpus())
            base = {"rows": [{"node_id": "n000000"}]}
            from darwinagent.operators.data import DataCapabilities

            total = len(DataCapabilities(graph).rows)
            out = stress_trial_samples([base], graph)
            self.assertTrue(any(p["rows"] == [] for p in out), "含空行集")
            self.assertTrue(
                any(len(p["rows"]) == total for p in out), "含全量规模行集（预算形态穷尽）"
            )
            self.assertTrue(
                any("日期" not in r for p in out for r in p["rows"][:1] if p["rows"]),
                "含缺字段形态",
            )
            self.assertGreater(len(out), 2, "含多形态")


class ContainerStringificationTests(unittest.TestCase):
    """Current restricted execution contract forbids container-to-string conversion."""

    def test_container_conversion_is_rejected(self):
        from darwinagent.operators.sandbox import Interpreter, Limits, admit

        src = "def run(params):\n return {'s': str(params['rows'][0].get('source_ids', []))}\n"
        fn = admit(src, "F", ["q?"])

        _caps = None  # 无能力依赖
        interp = Interpreter(fn, {}, Limits(30000, 15.0, 180000))
        from darwinagent.operators.sandbox import SandboxError

        with self.assertRaisesRegex(SandboxError, "Container-to-string conversion is unsupported"):
            interp.execute({"rows": [{"source_ids": ["D1:3", "D1:7"]}]})
