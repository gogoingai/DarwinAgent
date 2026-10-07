"""Recorded stability regressions for recovery."""

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
    CaseInput,
    EvaluationResult,
    QuestionInput,
    RunResult,
    SourceRef,
)
from darwinagent.experiments.recovery import (
    _retry_journal,
    _retryable_answer,
    _settle_reservations,
)
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.runtime.artifacts import atomic_json, digest
from tests.support.graphs import (
    corpus,
)
from tests.support.preflight import preflight_sync


class RecoveryTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_stage_mixed_fault_retries_only_transient_checkpoint(self):
        from darwinagent.experiments import stages as runner_module

        evidence = (SourceRef("m", "c", "1"),)
        initial = RunResult(
            "trial",
            "identity",
            "version",
            (
                AnswerResult("q1", "execution_error", "", error="TransportExhausted: connection"),
                AnswerResult(
                    "q2",
                    "execution_error",
                    "",
                    error="SandboxError: budget exhausted",
                    trace=({"stage": "tool_error"},),
                ),
                AnswerResult("q3", "answered", "ok", evidence=evidence),
            ),
            0,
        )
        after = RunResult(
            "trial",
            "identity",
            "version",
            (
                AnswerResult("q1", "answered", "ok", evidence=evidence),
                initial.answers[1],
                initial.answers[2],
            ),
            0,
        )

        class Client:
            def ledger_summary(self):
                return {"total_calls": 2}

            async def aclose(self):
                pass

        class Pipeline:
            calls = 0
            answers_dir = None

            def __init__(self, *_args, **_kwargs):
                pass

            async def run(self, *_args):
                type(self).calls += 1
                if type(self).calls == 1:
                    return initial
                self_test.assertFalse((self.answers_dir / f"{digest('q1')}.json").exists())
                self_test.assertTrue((self.answers_dir / f"{digest('q2')}.json").exists())
                self_test.assertTrue((self.answers_dir / f"{digest('q3')}.json").exists())
                return after

        class Evaluator:
            async def evaluate(self, _result, asked=None):
                return EvaluationResult({"m": 0}, 2, 3, 1, 0)

        async def no_sleep(_seconds):
            pass

        original_retry = runner_module.batched_fault_retry

        async def fast_retry(*args, **kwargs):
            kwargs["sleep"] = no_sleep
            return await original_retry(*args, **kwargs)

        self_test = self
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Pipeline.answers_dir = root / "R1" / "generation" / "trial" / "answers"
            for qid in ("q1", "q2", "q3"):
                atomic_json(Pipeline.answers_dir / f"{digest(qid)}.json", {"question_id": qid})
            case = CaseInput(
                "trial",
                corpus(),
                tuple(QuestionInput(qid, "问题" + qid) for qid in ("q1", "q2", "q3")),
            )
            runner = ExperimentRunner(
                None,
                lambda _client, _path: Evaluator(),
                None,
                RunConfig(),
                None,
                root,
                client_factory=lambda _stage: Client(),
            )
            spec = SimpleNamespace(bundle=SimpleNamespace(version="version"))
            with (
                mock.patch.object(runner_module, "Pipeline", Pipeline),
                mock.patch.object(runner_module, "batched_fault_retry", fast_retry),
            ):
                asyncio.run(runner._stage("R1", [case], spec))
            self.assertEqual(Pipeline.calls, 2)
            retry = json.loads((root / "R1" / "stage.json").read_text())["fault_retries"]["trial"]
            self.assertEqual(retry["retried"], 1)
            self.assertEqual(retry["skipped_deterministic"], 1)
            self.assertEqual(retry["still_faulted"], ["q2"])
            journal = json.loads((root / "R1" / "fault-retry" / "trial.json").read_text())
            self.assertEqual(set(journal["questions"]), {"q1"})
            self.assertEqual(journal["questions"]["q1"]["state"], "done")

    def test_stage_resume_does_not_reissue_reserved_mixed_fault(self):
        from darwinagent.experiments import stages as runner_module

        class Client:
            def ledger_summary(self):
                return {"total_calls": 4}

            async def aclose(self):
                pass

        class Pipeline:
            runs = 0

            def __init__(self, *args, **kwargs):
                pass

            async def run(self, *args):
                type(self).runs += 1
                return RunResult(
                    "trial",
                    "identity",
                    "version",
                    (
                        AnswerResult(
                            "q1", "execution_error", "", error="TransportExhausted: connection"
                        ),
                        AnswerResult("q2", "answered", "ok", evidence=(SourceRef("m", "c", "1"),)),
                    ),
                    0,
                )

        class Evaluator:
            async def evaluate(self, result, asked=None):
                return EvaluationResult({"m": 0}, 2, 2, 0, 0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = CaseInput(
                "trial", corpus(), (QuestionInput("q1", "问题一"), QuestionInput("q2", "问题二"))
            )
            journal = root / "R1" / "fault-retry" / "trial.json"
            atomic_json(
                journal,
                {
                    "identity": "identity",
                    "questions": {
                        "q1": {
                            "state": "reserved",
                            "attempts": 1,
                            "initial_error_type": "TransportExhausted",
                        }
                    },
                },
            )
            runner = ExperimentRunner(
                None,
                lambda client, path: Evaluator(),
                None,
                RunConfig(),
                None,
                root,
                client_factory=lambda stage: Client(),
            )
            spec = SimpleNamespace(bundle=SimpleNamespace(version="version"))
            with (
                mock.patch.object(runner_module, "Pipeline", Pipeline),
                mock.patch.object(runner_module, "batched_fault_retry") as retry,
            ):
                asyncio.run(runner._stage("R1", [case], spec))
                retry.assert_not_called()
            self.assertEqual(Pipeline.runs, 1)
            saved = json.loads(journal.read_text())["questions"]["q1"]
            self.assertEqual(saved["state"], "done")
            self.assertEqual(saved["attempts"], 1)
            self.assertEqual(saved["final_error_type"], "TransportExhausted")

    def test_mixed_faults_and_reserved_retry_settlement(self):
        transient = SimpleNamespace(
            question_id="transient",
            status="execution_error",
            error="TransportExhausted: connection",
            trace=(),
        )
        deterministic = SimpleNamespace(
            question_id="deterministic",
            status="execution_error",
            error="SandboxError: budget exhausted",
            trace=({"stage": "tool_error"},),
        )
        self.assertTrue(_retryable_answer(transient))
        self.assertFalse(_retryable_answer(deterministic))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retry.json"
            atomic_json(
                path,
                {
                    "identity": "run-v1",
                    "questions": {
                        "transient": {
                            "state": "reserved",
                            "attempts": 1,
                            "initial_digest": "before",
                        }
                    },
                },
            )
            result = SimpleNamespace(identity="run-v1", answers=(transient, deterministic))
            _settle_reservations(path, result, {"calls": 5})
            journal = _retry_journal(path, "run-v1")
            self.assertEqual(journal["questions"]["transient"]["state"], "done")
            self.assertEqual(journal["questions"]["transient"]["attempts"], 1)
            self.assertEqual(journal["questions"]["transient"]["consumed_after"], {"calls": 5})
            with self.assertRaises(ValueError):
                _retry_journal(path, "run-v2")
