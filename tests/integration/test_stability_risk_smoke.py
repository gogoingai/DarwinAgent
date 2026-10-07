"""Recorded stability regressions for risk smoke."""

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    AnswerResult,
    CaseInput,
    QuestionInput,
    SourceRef,
)
from darwinagent.experiments.runner import ExperimentRunner
from tests.support.graphs import (
    corpus,
)
from tests.support.preflight import preflight_sync


class RiskSmokeTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_candidate_smoke_keeps_each_risk_category_when_dates_dominate(self):
        from darwinagent.experiments import runner as runner_module

        class Client:
            async def aclose(self):
                pass

        class Pipeline:
            seen = ()

            def __init__(self, *_args, **_kwargs):
                pass

            async def run(self, case, *_args):
                type(self).seen = tuple(q.id for q in case.questions)
                return SimpleNamespace(
                    answers=(
                        AnswerResult(
                            "date0", "answered", "ok", evidence=(SourceRef("m", "c", "1"),)
                        ),
                    )
                )

        questions = tuple(QuestionInput(f"date{i}", f"昨天第{i}次") for i in range(8))
        questions += (
            QuestionInput("filter", "过滤全部结果"),
            QuestionInput("traverse", "关系遍历"),
        )
        case = CaseInput("trial", corpus(), questions)
        with tempfile.TemporaryDirectory() as td:
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(),
                None,
                Path(td),
                client_factory=lambda _stage: Client(),
            )
            with mock.patch.object(runner_module, "Pipeline", Pipeline):
                self.assertIsNone(
                    asyncio.run(runner._smoke_gate([case], SimpleNamespace(), candidate=True))
                )
        self.assertEqual(len(Pipeline.seen), 6)
        self.assertEqual(Pipeline.seen[:3], ("date0", "filter", "traverse"))

    def test_candidate_smoke_recovers_transient_but_blocks_tool_failure(self):
        from darwinagent.experiments import runner as runner_module

        @dataclass(frozen=True)
        class Case:
            id: str = "trial"
            questions: tuple = (QuestionInput("q1", "昨天有什么事实"),)

        class Client:
            async def aclose(self):
                pass

        class Pipeline:
            answer = None

            def __init__(self, *args, **kwargs):
                pass

            async def run(self, *args):
                return SimpleNamespace(answers=(self.answer,))

        async def recovered(*args, **kwargs):
            return SimpleNamespace(
                answers=(SimpleNamespace(question_id="q1", status="answered", error=None),)
            ), []

        with tempfile.TemporaryDirectory() as td:
            runner = ExperimentRunner(
                None, None, None, RunConfig(), None, Path(td), client_factory=lambda stage: Client()
            )
            spec = SimpleNamespace()
            Pipeline.answer = SimpleNamespace(
                question_id="q1",
                status="execution_error",
                error="TransportExhausted: connection",
                trace=(),
            )
            with (
                mock.patch.object(runner_module, "Pipeline", Pipeline),
                mock.patch.object(
                    runner_module, "batched_fault_retry", side_effect=recovered
                ) as retry,
            ):
                self.assertIsNone(asyncio.run(runner._smoke_gate([Case()], spec, candidate=True)))
                self.assertEqual(retry.call_count, 1)
            Pipeline.answer = SimpleNamespace(
                question_id="q1",
                status="execution_error",
                error="SandboxError: budget exhausted",
                trace=({"stage": "tool_error"},),
            )
            with (
                mock.patch.object(runner_module, "Pipeline", Pipeline),
                mock.patch.object(runner_module, "batched_fault_retry") as retry,
            ):
                self.assertIn(
                    "候选冒烟执行故障",
                    asyncio.run(runner._smoke_gate([Case()], spec, candidate=True)),
                )
                retry.assert_not_called()
