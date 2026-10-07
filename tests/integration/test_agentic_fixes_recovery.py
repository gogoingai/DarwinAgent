"""Offline regression scenarios for recovery."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    AnswerResult,
    EvaluationResult,
    QuestionInput,
    RunResult,
    SourceRef,
)
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.runtime.artifacts import digest


class BatchedFaultRetryTests(unittest.TestCase):
    def run_retry(self, script, faulted):
        from darwinagent.experiments.recovery import batched_fault_retry

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        answers_dir = Path(td.name)
        for a in faulted:
            (answers_dir / f"{digest(a.question_id)}.json").write_text("{}")
        calls = []

        class FakePipeline:
            async def run(self, case, spec, config):
                calls.append(1)
                return script.pop(0)

        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        result, still = asyncio.run(
            batched_fault_retry(
                FakePipeline(),
                "c",
                None,
                None,
                answers_dir,
                faulted,
                sleep=fake_sleep,
                batch_size=25,
                lead_s=0.0,
                gap_s=0.0,
            )
        )
        return result, still, calls, sleeps, answers_dir

    def test_all_recovered_final_set_recomputed(self):
        # 评审#4 离线复现场景：30 题故障，第一批重跑后其余 5 题仍带旧故障检查点，
        # 全部重跑后恢复。最终统计必须出自最后一份答案集（旧实现取并集误报 5 题）。
        faulted = [AnswerResult(f"q{i}", "execution_error", "", error="x") for i in range(30)]
        ev = (SourceRef("message_text", "c", "1"),)
        healthy = [AnswerResult(f"q{i}", "answered", "ok", evidence=ev) for i in range(30)]
        rerun1 = RunResult("c", "id", "v", tuple(healthy[:25] + faulted[25:]), 0)
        rerun2 = RunResult("c", "id", "v", tuple(healthy), 0)
        result, still, calls, sleeps, answers_dir = self.run_retry([rerun1, rerun2], faulted)
        self.assertEqual(calls, [1, 1])  # 两批各重跑一次
        self.assertEqual(still, [])  # 并集实现会留下 q25..q29
        self.assertEqual(result, rerun2)  # 返回最后一份完整答案集
        self.assertEqual(sleeps, [0.0, 0.0])  # lead+gap 各一次（均为 0 秒）
        self.assertFalse(any(answers_dir.glob("*.json")))  # 当前故障视图已移除，原件仍保留
        historical = list((answers_dir / "history").glob("*/*.json"))
        self.assertEqual(len(historical), 30)
        self.assertTrue(all(path.read_bytes() == b"{}" for path in historical))

    def test_archive_failure_keeps_current_failed_checkpoint(self):
        from darwinagent.experiments.recovery import batched_fault_retry

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{digest('q1')}.json"
            original = b'{ "old_failure": "precise bytes" }\n'
            path.write_bytes(original)
            with mock.patch(
                "darwinagent.runtime.continuation._immutable_copy", side_effect=OSError("disk")
            ):
                with self.assertRaises(OSError):
                    asyncio.run(
                        batched_fault_retry(
                            mock.Mock(),
                            "c",
                            None,
                            None,
                            Path(tmp),
                            [AnswerResult("q1", "execution_error", "", error="x")],
                            lead_s=0,
                        )
                    )
            self.assertEqual(path.read_bytes(), original)

    def test_persistent_faults_reported_from_final_set(self):
        faulted = [
            AnswerResult("q1", "execution_error", "", error="x"),
            AnswerResult("q2", "execution_error", "", error="y"),
        ]
        final = RunResult(
            "c",
            "id",
            "v",
            (
                AnswerResult("q1", "answered", "ok", evidence=(SourceRef("m", "c", "1"),)),
                AnswerResult("q2", "execution_error", "", error="y"),
            ),
            0,
        )
        result, still, *_ = self.run_retry([final], faulted)
        self.assertEqual(still, ["q2"])
        self.assertEqual(result, final)


class FaultRetryInvalidatesEvaluationTests(unittest.TestCase):
    def test_recovered_answers_force_re_evaluation(self):
        # 评审#4：重试跑过＝旧评测检查点作废重评；恢复题必须被重新评分，
        # 不得沿用基于故障答案集的旧检查点
        import asyncio
        from types import SimpleNamespace

        from darwinagent.experiments import stages as R

        class FakeClient:
            async def aclose(self):
                pass

            def ledger_summary(self):
                return {"total_calls": 0}

        ev = (SourceRef("message_text", "c", "1"),)
        faulted = RunResult(
            "c",
            "i",
            "v",
            (
                AnswerResult("q1", "execution_error", "", error="TransportExhausted: connection"),
                AnswerResult("q2", "execution_error", "", error="TransportExhausted: rate limit"),
            ),
            0,
        )
        recovered = RunResult(
            "c",
            "i",
            "v",
            (
                AnswerResult("q1", "answered", "ok", evidence=ev),
                AnswerResult("q2", "answered", "ok", evidence=ev),
            ),
            0,
        )
        scripted = [faulted, recovered]

        class FakePipeline:
            def __init__(self, client, work_dir, frozen_snapshot=None, graph_builder=None):
                pass

            async def run(self, case, spec, config):
                return scripted.pop(0)

        evaluated = []

        class FakeEvaluator:
            def __init__(self, client, path):
                pass

            async def evaluate(self, result, asked=None):
                evaluated.append([a.status for a in result.answers])
                return EvaluationResult({"m": 1}, 2, 2, 0, 0)

        async def fake_sleep(_s):
            pass

        original_retry = R.batched_fault_retry

        async def fast_retry(*args, **kwargs):
            kwargs["sleep"] = fake_sleep
            return await original_retry(*args, **kwargs)

        class C:
            id = "c"
            questions = (QuestionInput("q1", "?"), QuestionInput("q2", "?"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 基于故障答案集的陈旧评测检查点（身份匹配，若不作废将被直接复用）
            stale = EvaluationResult({"m": 0}, 0, 2, 2, 0)
            (root / "B0" / "evaluation").mkdir(parents=True)
            (root / "B0" / "evaluation" / "c.json").write_text(
                json.dumps({"run_identity": "i", "asset_version": "v", "scores": stale.to_dict()})
            )
            runner = ExperimentRunner(
                type("Adapter", (), {"generation_input": staticmethod(lambda ident: C())})(),
                lambda client, path: FakeEvaluator(client, path),
                None,
                RunConfig(protocol_attempts=1),
                None,
                root,
                client_factory=lambda stage: FakeClient(),
            )
            spec = SimpleNamespace(bundle=SimpleNamespace(version="v"))
            with (
                mock.patch.object(R, "Pipeline", FakePipeline),
                mock.patch.object(R, "batched_fault_retry", fast_retry),
            ):
                results, scores = asyncio.run(runner._stage("B0", [C()], spec))
            self.assertEqual(scores.completed, 2)
            self.assertEqual(
                evaluated, [["answered", "answered"]]
            )  # 陈旧检查点已作废、恢复题被重评
            self.assertEqual(results[0].answers[0].status, "answered")


class DeterministicFaultTests(unittest.TestCase):
    """确定性工具错误不整题重试（评审：接口/参数错误重试不会变好）：
    SandboxError 类故障跳过分批重试，如实记录进 skipped_retry。"""

    def test_deterministic_error_skips_retry(self):
        import asyncio

        import darwinagent.experiments.stages as R

        scripted = [
            RunResult(
                "c",
                "i",
                "v",
                (
                    AnswerResult("q1", "answered", "a", (SourceRef("k", "d", "l"),)),
                    AnswerResult(
                        "q2",
                        "execution_error",
                        "",
                        error="SandboxError: Restricted execution failed",
                    ),
                ),
                (),
                (),
            )
        ]
        calls = []

        class FakePipeline:
            def __init__(self, client, work_dir, frozen_snapshot=None, graph_builder=None):
                pass

            async def run(self, case, spec, config):
                calls.append(1)
                return scripted[0]

        class FakeEvaluator:
            def __init__(self, client, path):
                pass

            async def evaluate(self, result, asked=None):
                return EvaluationResult({"m": 1}, 2, 2, 0, 0)

        class StubClient:
            async def aclose(self):
                pass

            def ledger_summary(self):
                return {}

        class C:
            id = "c"
            questions = (QuestionInput("q1", "?"), QuestionInput("q2", "?"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = ExperimentRunner(
                type("Adapter", (), {"generation_input": staticmethod(lambda i: C())})(),
                lambda c, p: FakeEvaluator(c, p),
                None,
                RunConfig(protocol_attempts=1),
                None,
                root,
                client_factory=lambda s: StubClient(),
            )
            spec = SimpleNamespace(bundle=SimpleNamespace(version="v"))
            import contextlib
            import io

            with (
                mock.patch.object(R, "Pipeline", FakePipeline),
                mock.patch.object(
                    R, "batched_fault_retry", side_effect=AssertionError("不应触发重试")
                ) as _no_retry,
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                results, scores = asyncio.run(runner._stage("B0", [C()], spec))
            self.assertEqual(calls, [1])  # 只跑一次：确定性错误未重试
            self.assertIn("deterministic_tool_error", out.getvalue())
