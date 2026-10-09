"""Offline regression scenarios for domain evaluation."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.contracts import (
    AnswerResult,
    CaseInput,
    QuestionInput,
)
from darwinagent.kernel.assets import Asset, KernelAssets
from tests.support.artifacts import _fake_dual_grade_batch


class ExternalReportTests(unittest.TestCase):
    def test_macro_micro_and_delta_use_rates(self):
        # 评审#1 反例：A 200 题（融合 100/向量 90），B 100 题（融合 80/向量 90）
        # 融合宏平均 65% vs 向量 67.5% → 差值 −2.5pp；按题汇总两者同为 60%（计数平均会误报 0）
        from datasets.locomo.scripts.external_test import aggregate_report

        g1 = [
            {
                "case_id": "A",
                "total": 200,
                "completed": 200,
                "original_precise": 100,
                "original_lenient": 110,
                "repaired_precise": 101,
                "repaired_lenient": 111,
                "generation_faults": 0,
                "evaluation_faults": 0,
            },
            {
                "case_id": "B",
                "total": 100,
                "completed": 100,
                "original_precise": 80,
                "original_lenient": 90,
                "repaired_precise": 81,
                "repaired_lenient": 91,
                "generation_faults": 0,
                "evaluation_faults": 0,
            },
        ]
        v0 = [
            {
                "case_id": "A",
                "total": 200,
                "completed": 195,
                "original_precise": 90,
                "original_lenient": 100,
                "repaired_precise": 90,
                "repaired_lenient": 100,
                "generation_faults": 5,
                "evaluation_faults": 0,
            },
            {
                "case_id": "B",
                "total": 100,
                "completed": 100,
                "original_precise": 90,
                "original_lenient": 95,
                "repaired_precise": 90,
                "repaired_lenient": 95,
                "generation_faults": 0,
                "evaluation_faults": 0,
            },
        ]
        report = aggregate_report(g1, v0)
        self.assertEqual(report["macro"]["original_precise_rate"], 65.0)  # (50%+80%)/2
        self.assertEqual(report["macro"]["original_precise_rate"], 65.0)
        self.assertEqual(report["micro"]["original_precise_correct"], 180)  # 原始答对题数保留
        self.assertEqual(report["micro"]["total_sum"], 300)
        self.assertEqual(report["micro"]["original_precise_rate"], 60.0)  # 180/300 按题汇总
        self.assertEqual(report["micro"]["generation_faults"], 0)
        deltas = {d["case_id"]: d for d in report["delta_vs_baseline"]}
        self.assertEqual(deltas["A"]["original_precise_delta_pp"], 5.0)  # 50%−45%
        self.assertEqual(deltas["B"]["original_precise_delta_pp"], -10.0)  # 80%−90%
        self.assertEqual(report["macro"]["delta_vs_baseline_pp"]["original_precise"], -2.5)
        # 基线侧故障单独报告：分母不减故障题
        baseline_report = aggregate_report(v0)
        self.assertEqual(baseline_report["micro"]["total_sum"], 300)
        self.assertEqual(baseline_report["micro"]["generation_faults"], 5)


class ExternalEntryTests(unittest.TestCase):
    """评审一：真实外测入口的离线端到端——加载锁定资产、逐对话预检（挂向量索引＋图 C
    否决＋能力试跑）到报告生成，不只测 aggregate_report。"""

    def _setup(self, td, with_traverse=True, bad_c=False):
        from darwinagent.engine import Pipeline  # noqa: F401  确认入口依赖可导入
        from darwinagent.kernel import TaskSpec
        from tests.support.graphs import (
            ROOT,
            build_snapshot,
            cold_bundle,
            corpus,
        )

        root = Path(td)
        snapshot, manifest = build_snapshot(root)
        bundle = cold_bundle(root)
        assets = list(bundle.assets.assets)
        if with_traverse:
            assets.append(
                Asset(
                    "f_traverse",
                    "F",
                    "def run(params):\n return traverse(params['node_id'], params['relation'])\n",
                    {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string"},
                            "relation": {"type": "string"},
                        },
                    },
                    {"type": "array"},
                    trial_inputs=({"node_id": "n000000", "relation": "归属于"},),
                    schema_dependencies=["schema"],
                    description="关系遍历",
                )
            )
        if bad_c:
            assets.append(
                Asset(
                    "c_bad",
                    "C",
                    "def check(candidate):\n return {'ok': False, 'issues': ['图检查否决样例']}",
                    {"type": "any"},
                    {"type": "any"},
                    schema_dependencies=["schema"],
                    stage="graph",
                    description="坏检查",
                )
            )
        locked = KernelAssets(tuple(assets)).export(root / "locked")
        task = TaskSpec.load(ROOT / "tasks/conversation_memory/task.yaml")
        case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "甲计划做什么？"),))
        return (
            root,
            snapshot,
            manifest,
            locked,
            task,
            {"conv-x": case},
        )  # export() 已返回 KernelBundle

    def test_entry_end_to_end_offline(self):
        import tempfile

        from datasets.locomo.scripts.external_test import (
            aggregate_report,
            baseline_compatibility,
            experiment_identity,
            preflight,
        )
        from tests.support.graphs import FakeEmbedder

        with tempfile.TemporaryDirectory() as td:
            root, snapshot, manifest, bundle, task, cases = self._setup(td)
            config = __import__("darwinagent.config", fromlist=["RunConfig"]).RunConfig(
                function_timeout_s=15.0
            )
            # 完整入口第一段：锁定资产加载 + 逐对话预检（真向量索引挂载＋图 C＋能力试跑）
            preflight(
                bundle,
                config,
                task,
                cases,
                snap_root=root / "snapshots",
                embedder_factory=lambda: FakeEmbedder(),
            )
            # 报告段：逐对话行含正确率字段，身份齐备，兼容性判定生效
            rows = [
                {
                    "case_id": "conv-x",
                    "total": 1,
                    "completed": 1,
                    "original_precise": 1,
                    "original_lenient": 1,
                    "repaired_precise": 1,
                    "repaired_lenient": 1,
                    "generation_faults": 0,
                    "evaluation_faults": 0,
                }
            ]
            ident = experiment_identity(
                bundle, config, None, ["conv-x"], snap_root=root / "snapshots"
            )
            self.assertEqual(ident["snapshots"]["conv-x"], manifest["snapshot_digest"])
            self.assertEqual(baseline_compatibility(ident, dict(ident)), "compatible")
            report = aggregate_report(rows, rows)
            self.assertEqual(report["macro"]["original_precise_rate"], 100.0)

    def test_entry_rejects_missing_capability_and_bad_c(self):
        import tempfile

        from datasets.locomo.scripts.external_test import preflight
        from tests.support.graphs import FakeEmbedder

        config = __import__("darwinagent.config", fromlist=["RunConfig"]).RunConfig(
            function_timeout_s=15.0
        )
        with tempfile.TemporaryDirectory() as td:
            root, _, _, bundle, task, cases = self._setup(td, with_traverse=False)
            with self.assertRaises(SystemExit) as caught:
                preflight(
                    bundle,
                    config,
                    task,
                    cases,
                    snap_root=root / "snapshots",
                    embedder_factory=lambda: FakeEmbedder(),
                )
            self.assertIn("静态能力底线", str(caught.exception))
        with tempfile.TemporaryDirectory() as td:
            root, _, _, bundle, task, cases = self._setup(td, bad_c=True)
            with self.assertRaises(SystemExit) as caught:
                preflight(
                    bundle,
                    config,
                    task,
                    cases,
                    snap_root=root / "snapshots",
                    embedder_factory=lambda: FakeEmbedder(),
                )
            self.assertIn("图检查否决样例", str(caught.exception))  # 评审二：否决必须被采纳


class TrimmedEvaluateTests(unittest.TestCase):
    """训练集瘦身事故（v9 B0 全灭）：evaluator 完整性检查曾硬性要求全会话答案集，
    瘦身到 100 题后在判分入口崩溃。冒烟门走 dual_grade_batch 子集、不经过这个检查，
    所以没拦住。asked 语义＝按本轮实际出题集核对；None 保持全会话要求。"""

    def _evaluator(self, n_qas, audited=False):
        from dataclasses import dataclass

        import datasets.locomo.evaluator as ev

        @dataclass
        class Q:
            idx: int
            question: str
            answer: str = ""

        conv = SimpleNamespace(qas=[Q(i, f"q{i}") for i in range(n_qas)])
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            lock = tdp / "lock.json"
            lock.write_text("{}")
            aud = None
            if audited:
                aud = tdp / "audited.json"
                aud.write_text(
                    json.dumps(
                        [
                            {"idx": i, "question": f"q{i}", "answer": f"g{i}", "disputed": i == 5}
                            for i in range(n_qas)
                        ]
                    )
                )
            evaluator = ev.LocomoEvaluator(
                None,
                tdp / "work",
                dataset_path="explicit.json",
                audited_path=aud or tdp / "x.json",
                lock_path=lock,
            )

            def fake_aggregate(rows, disputed):
                return {
                    "overall": {
                        "lenient": {"correct": len(rows)},
                        "precise": {"correct": len(rows)},
                    },
                    "grades": [{"idx": row["idx"], "status": "ok"} for row in rows],
                }

            with (
                mock.patch.object(ev, "verify_files"),
                mock.patch.object(ev, "load_conversation", return_value=conv),
                mock.patch.object(ev, "transcript", return_value=""),
                mock.patch.object(ev, "aggregate", fake_aggregate),
                mock.patch.object(ev, "dual_grade_batch", new=_fake_dual_grade_batch),
            ):
                yield evaluator

    def test_asked_subset_passes_and_grades_only_asked(self):
        gen = self._evaluator(199)
        evaluator = next(gen)
        answers = tuple(
            AnswerResult(question_id=str(i), status="abstained", answer="记忆中无支持", evidence=())
            for i in range(100)
        )
        result = SimpleNamespace(case_id="conv-99", answers=answers)
        scores = asyncio.run(evaluator.evaluate(result, asked=tuple(range(100))))
        self.assertEqual(scores.total, 100)
        self.assertEqual(scores.metrics["original_precise"], 100)
        self.assertEqual(len(scores.diagnostics), 100)

    def test_full_set_still_required_without_asked(self):
        gen = self._evaluator(199)
        evaluator = next(gen)
        answers = tuple(
            AnswerResult(question_id=str(i), status="abstained", answer="记忆中无支持", evidence=())
            for i in range(100)
        )
        result = SimpleNamespace(case_id="conv-99", answers=answers)
        with self.assertRaisesRegex(ValueError, "Complete independent answer set"):
            asyncio.run(evaluator.evaluate(result))

    def test_missing_answer_within_asked_still_rejected(self):
        gen = self._evaluator(199)
        evaluator = next(gen)
        answers = tuple(
            AnswerResult(question_id=str(i), status="abstained", answer="记忆中无支持", evidence=())
            for i in range(99)
        )
        result = SimpleNamespace(case_id="conv-99", answers=answers)
        with self.assertRaisesRegex(ValueError, "Complete independent answer set"):
            asyncio.run(evaluator.evaluate(result, asked=tuple(range(100))))

    def test_audited_gold_aligned_to_asked_subset(self):
        gen = self._evaluator(199, audited=True)
        evaluator = next(gen)
        answers = tuple(
            AnswerResult(question_id=str(i), status="abstained", answer="记忆中无支持", evidence=())
            for i in range(100)
        )
        result = SimpleNamespace(case_id="conv-26", answers=answers)
        scores = asyncio.run(evaluator.evaluate(result, asked=tuple(range(100))))
        self.assertEqual(scores.total, 100)
        self.assertIn("repaired_precise", scores.metrics)
        self.assertTrue(all("repaired" in row for row in scores.diagnostics))
