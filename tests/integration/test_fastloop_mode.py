"""新模式（recheck4「冻结记忆/向量、图可重建」快速循环）离线合同测试：无模型。

覆盖：轮预算退出（deadline 不随重试重置、超时持久化且不计正式轮）、提案数上限、
P.extract 补丁标注未生效、验证聚合选版（退化即拒、聚合落盘、逐题反馈不进提案器）、
重建图供给缓存（同 S 复用、S 变重建）。真实模型短验由
docs/diagnostics/wiki-fastloop-20261005/ 驱动脚本承担。
"""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oak.config import RunConfig
from oak.experiments.proposal import ProposalGenerator
from oak.experiments.runner import ExperimentRunner
from oak.kernel import TaskSpec
from oak.kernel.registration import load_assets
from oak.contracts import EvaluationResult
from tests.fixtures import TASK
from tests.integration.test_experiment import (LedgerRecordedClient,
                                               RecordedExperiment, client)
from tests.integration.test_wiki_optimization import WikiRecordedExperiment


class FastLoopExperiment(WikiRecordedExperiment):
    """记录式全环＋新模式参数（graph_builder 等在 super().__init__ 后注入——
    测试绕过构造校验，运行路径按属性生效）。"""

    def __init__(self, root, evaluator=None, **mode):
        RecordedExperiment.__init__(self, root, evaluator=evaluator)
        self.optimization_mode = "wiki"
        for key, value in mode.items():
            setattr(self, key, value)
        self._round_deadline = None
        self._rebuild_cache = {}


def _run(runner, rounds=1):
    with contextlib.redirect_stdout(io.StringIO()):
        return asyncio.run(runner.run(
            runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=rounds,
            scope=("S", "F", "C", "P")))


class RoundBudgetTests(unittest.TestCase):
    def test_round_deadline_persists_and_is_not_formal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=0.001)
            original = ProposalGenerator.propose

            async def fail_first(self, *args, **kwargs):
                if "attempt-0" in str(args[5]):
                    raise ValueError("static candidate shape invalid")
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, "propose", fail_first):
                summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            self.assertFalse(decision["accepted"])
            self.assertEqual(decision["status"], "validation_failed")
            self.assertTrue(any("Round deadline" in r for r in decision["reasons"]),
                            decision["reasons"])
            state = json.loads(
                (root / "R1/optimization/attempt-1/status.json").read_text())
            self.assertEqual(state["state"], "deadline")
            self.assertIn("deadline exceeded", state["error"])

    def test_proposal_attempts_cap_exhausts_before_fifty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, proposal_attempts=1)
            original = ProposalGenerator.propose

            async def fail_first(self, *args, **kwargs):
                if "attempt-0" in str(args[5]):
                    raise ValueError("static candidate shape invalid")
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, "propose", fail_first):
                summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            self.assertEqual(decision["status"], "validation_failed")
            self.assertTrue(any("WikiAdmissionExhausted" in r for r in decision["reasons"]))
            first = json.loads((root / "R1/optimization/attempt-0/status.json").read_text())
            self.assertEqual(first["state"], "failed")
            self.assertFalse((root / "R1/optimization/attempt-1").exists())


class ValidationSelectionTests(unittest.TestCase):
    @staticmethod
    def _stub_evaluator(transport, path):
        stage = Path(path)
        name = stage.parent.name
        if not (name == "B0" or name.startswith("R")):
            name = stage.parent.parent.name

        class _Eval:
            async def evaluate(self, result, asked=None):
                assert result.answers, "validation run produced no answers"
                if name.endswith("-val"):
                    base = name == "B0-val"
                    precise = 2 if base else 1
                    return EvaluationResult(
                        {"original_precise": precise, "original_lenient": precise},
                        2, 2, 0, 0)
                return EvaluationResult(
                    {"precise": 0 if name == "B0" else 1}, 1, 1, 0, 0)
        return _Eval()

    def test_validation_regression_rejects_and_aggregates_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, evaluator=self._stub_evaluator,
                                        validation_plan={
                                            "case": None,  # 占位：run 前替换为 recorded case
                                            "policy": __import__(
                                                "oak.experiments.spec", fromlist=["SelectionPolicy"]
                                            ).SelectionPolicy("original_precise", "original_lenient")})
            runner.validation_plan["case"] = runner.case
            summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            # 训练主判本应接受（B0 precise 0 → R1 precise 1），验证退化（2→1）翻转拒绝
            self.assertFalse(decision["accepted"], decision)
            self.assertTrue(any(str(r).startswith("validation_") for r in decision["reasons"]),
                            decision["reasons"])
            self.assertEqual(decision["validation"]["metrics"]["original_precise"], 1)
            history = json.loads((root / "validation.json").read_text())
            self.assertIn("B0", history)
            self.assertIn("R1", history)
            self.assertEqual(history["B0"]["metrics"]["original_precise"], 2)
            # 聚合进 wiki formal（无逐题诊断）
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            formal = next(e for e in wiki["entries"]
                          if e["stage"] == "R1" and e["kind"] == "formal")
            self.assertIn("validation", formal["facts"])
            self.assertEqual(formal["facts"]["validation"]["metrics"]["original_precise"], 1)

    def test_validation_improvement_still_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def improving(transport, path):
                stage = Path(path)
                name = stage.parent.name
                if not (name == "B0" or name.startswith("R")):
                    name = stage.parent.parent.name

                class _Eval:
                    async def evaluate(self, result, asked=None):
                        if name.endswith("-val"):
                            precise = 1 if name == "B0-val" else 2
                            return EvaluationResult(
                                {"original_precise": precise, "original_lenient": precise},
                                2, 2, 0, 0)
                        return EvaluationResult(
                            {"precise": 0 if name == "B0" else 1}, 1, 1, 0, 0)
                return _Eval()

            runner = FastLoopExperiment(root, evaluator=improving, validation_plan={
                "case": None,
                "policy": __import__("oak.experiments.spec",
                                     fromlist=["SelectionPolicy"]).SelectionPolicy(
                                         "original_precise", "original_lenient")})
            runner.validation_plan["case"] = runner.case
            summary = _run(runner, rounds=1)
            self.assertTrue(summary["rounds"][0]["accepted"], summary["rounds"][0])


class PExtractMarkingTests(unittest.TestCase):
    def test_extract_patch_marked_not_effective_in_rebuild_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class ExtractPatcher(FastLoopExperiment):
                patched = False

                def _client(self, stage):
                    transport = super()._client(stage)
                    replies = getattr(transport, "replies", None)
                    if (stage.startswith("R") and replies and "proposal" in replies
                            and not self.patched):
                        self.patched = True
                        pointer = json.loads(
                            (self.root / "published/current.json").read_text())
                        from oak.kernel import KernelBundle
                        base = KernelBundle(self.root / "published" / pointer["path"])
                        asset = next(a for a in base.assets.assets if a.role == "extract")
                        updated = asset.to_dict()
                        updated["content"] += "\nReweight extraction emphasis."
                        from collections import deque
                        from oak.kernel.revision import training_id
                        replies["proposal"] = deque([
                            {"patches": [{"asset": updated,
                                          "base_fingerprint": asset.fingerprint,
                                          "reason": "extract emphasis",
                                          "training_evidence": [
                                              training_id(self.case.id,
                                                          self.case.questions[0].id)]}]}])
                    return transport

            runner = ExtractPatcher(root, graph_builder=object())
            summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            self.assertIn("p_extract_not_effective", decision, decision)
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            formal = next(e for e in wiki["entries"]
                          if e["stage"] == "R1" and e["kind"] == "formal")
            self.assertIn("p_extract_not_effective", formal["facts"])


class RebuildSupplyCacheTests(unittest.TestCase):
    def test_rebuild_cache_reuses_same_schema_and_rebuilds_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FastLoopExperiment(Path(tmp), graph_builder=lambda *a: object())
            from oak.kernel.registration import load_assets as la
            bundle = la(TASK).export(Path(tmp) / "bundle")
            calls = []

            def counting(snapshot_dir, schema, corpus=(), embedder_factory=None):
                calls.append(schema.to_yaml()[:40])
                return object()

            runner.graph_builder = counting
            from types import SimpleNamespace
            case = SimpleNamespace(id="conv-26")
            runner.snapshot_root = Path("datasets/locomo/snapshots/gvtest_v1")
            fake_schema = mock.MagicMock()
            fake_schema.to_yaml.return_value = "schema-v1"
            with mock.patch("oak.kernel.validation.validate_bundle",
                            return_value=fake_schema):
                runner._rebuild_graph_cached(bundle, case)
                runner._rebuild_graph_cached(bundle, case)
                self.assertEqual(len(calls), 1)  # 同 S → 复用
                fake_schema.to_yaml.return_value = "schema-v2"
                runner._rebuild_graph_cached(bundle, case)
                self.assertEqual(len(calls), 2)  # S 变 → 重建


if __name__ == "__main__":
    unittest.main()
