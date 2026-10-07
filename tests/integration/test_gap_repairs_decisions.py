"""Offline regression scenarios for decisions."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.kernel import TaskSpec
from tests.support.device import TASK
from tests.support.recorded_experiment import RecordedExperiment


class ScoredRoundsAndIdentity(unittest.TestCase):
    """①：计分轮口径＋rounds 移出身份（含旧声明 rounds 键兼容）。"""

    @staticmethod
    def _lenient_evaluator(transport, path):
        stage = Path(path)
        name = stage.parent.name
        if not (name == "B0" or name.startswith("R")):
            name = stage.parent.parent.name

        class _Eval:
            async def evaluate(self, result, asked=None):
                from darwinagent.contracts import EvaluationResult

                precise = 0 if name == "B0" else 1
                return EvaluationResult({"precise": precise}, 1, 1, 0, 0)

        return _Eval()

    def test_timeout_iteration_does_not_consume_round(self):
        import asyncio as _aio

        from tests.support.recorded_fastloop import FastLoopExperiment, _run

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(
                root,
                round_deadline_s=2.0,
                evaluator=self._lenient_evaluator,
                validation_plan={"case": None, "policy": None},
            )
            runner.validation_plan["case"] = runner.case
            from darwinagent.experiments.spec import SelectionPolicy

            runner.validation_plan["policy"] = SelectionPolicy("precise", "precise")
            from darwinagent.experiments.proposal import ProposalGenerator

            original = ProposalGenerator.propose

            async def stall_r1_then_fail(self, *args, **kwargs):
                if "R1" in str(args[5]):
                    await _aio.sleep(2.1)  # 越过 2.0s 轮预算 → attempt-0 失败后再查即超时
                    raise ValueError("static candidate shape invalid")
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, "propose", stall_r1_then_fail):
                summary = _run(runner, rounds=1)
            # R1 超时不占轮数；循环继续到 R2 计分完成（scored=1 即止）
            statuses = [
                (r.get("status"), bool((r.get("candidate") or {}).get("metrics")))
                for r in summary["rounds"]
            ]
            self.assertEqual(statuses[0][0], "round_timeout")
            scored = sum(1 for _, s in statuses if s)
            self.assertGreaterEqual(scored, 1, statuses)

    def test_legacy_declaration_with_rounds_key_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner_cls = type("SeededOnce", (RecordedExperiment,), {"optimization_mode": "wiki"})
            r1 = runner_cls(root)
            spec = TaskSpec.load(TASK / "task.yaml")
            with contextlib.redirect_stdout(io.StringIO()):
                s1 = asyncio.run(r1.run(r1.case.id, spec, rounds=1, scope=("S", "F", "C", "P")))
            self.assertEqual(s1["status"], "complete")
            # 旧格式声明（含 rounds 键）在移除 rounds 出身份后仍可续跑
            ep = root / "experiment.json"
            decl = json.loads(ep.read_text())
            decl["rounds"] = 1
            ep.write_text(json.dumps(decl, ensure_ascii=False, default=str))
            r2 = runner_cls(root)
            with contextlib.redirect_stdout(io.StringIO()):
                s2 = asyncio.run(
                    r2.run(r2.case.id, spec, rounds=2, resume=True, scope=("S", "F", "C", "P"))
                )
            self.assertEqual(len(s2["rounds"]), 2, s2["rounds"])
            self.assertNotIn("rounds", json.loads(ep.read_text()))
