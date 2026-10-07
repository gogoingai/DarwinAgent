"""Offline regression scenarios for validation selection."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.contracts import EvaluationResult
from darwinagent.kernel import TaskSpec
from tests.support.device import TASK
from tests.support.recorded_fastloop import FastLoopExperiment, _run


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
                        {"original_precise": precise, "original_lenient": precise}, 2, 2, 0, 0
                    )
                return EvaluationResult({"precise": 0 if name == "B0" else 1}, 1, 1, 0, 0)

        return _Eval()

    def test_validation_regression_rejects_and_aggregates_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(
                root,
                evaluator=self._stub_evaluator,
                validation_plan={
                    "case": None,  # 占位：run 前替换为 recorded case
                    "policy": __import__(
                        "darwinagent.experiments.spec", fromlist=["SelectionPolicy"]
                    ).SelectionPolicy("original_precise", "original_lenient"),
                },
            )
            runner.validation_plan["case"] = runner.case
            summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            # 训练主判本应接受（B0 precise 0 → R1 precise 1），验证退化（2→1）翻转拒绝
            self.assertFalse(decision["accepted"], decision)
            self.assertTrue(
                any(str(r).startswith("validation_") for r in decision["reasons"]),
                decision["reasons"],
            )
            self.assertEqual(decision["validation"]["metrics"]["original_precise"], 1)
            history = json.loads((root / "validation.json").read_text())
            self.assertIn("B0", history)
            self.assertIn("R1", history)
            self.assertEqual(history["B0"]["metrics"]["original_precise"], 2)
            # 聚合进 wiki formal（无逐题诊断）
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            formal = next(
                e for e in wiki["entries"] if e["stage"] == "R1" and e["kind"] == "formal"
            )
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
                                2,
                                2,
                                0,
                                0,
                            )
                        return EvaluationResult(
                            {"precise": 0 if name == "B0" else int(name[1:])}, 2, 2, 0, 0
                        )

                return _Eval()

            runner = FastLoopExperiment(
                root,
                evaluator=improving,
                validation_plan={
                    "case": None,
                    "policy": __import__(
                        "darwinagent.experiments.spec", fromlist=["SelectionPolicy"]
                    ).SelectionPolicy("original_precise", "original_lenient"),
                },
            )
            runner.validation_plan["case"] = runner.case
            summary = _run(runner, rounds=2)
            self.assertTrue(summary["rounds"][0]["accepted"], summary["rounds"][0])
            self.assertFalse(summary["rounds"][1]["accepted"])
            # Simulate interruption between the R2 score and terminal decision.
            # R2 must still compare with adopted R1 validation, not B0.
            (root / "R2/decision.json").unlink()
            restored = FastLoopExperiment(
                root, evaluator=improving, validation_plan=runner.validation_plan
            )
            with contextlib.redirect_stdout(io.StringIO()):
                resumed = asyncio.run(
                    restored.run(
                        restored.case.id,
                        TaskSpec.load(TASK / "task.yaml"),
                        rounds=2,
                        resume=True,
                        scope=("S", "F", "C", "P"),
                    )
                )
            self.assertFalse(resumed["rounds"][1]["accepted"])
            self.assertIn(
                "validation_primary_not_strictly_improved", resumed["rounds"][1]["reasons"]
            )
            history = json.loads((root / "validation.json").read_text())
            self.assertEqual(history["R1"]["metrics"]["original_precise"], 2)
