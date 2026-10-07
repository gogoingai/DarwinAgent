"""Offline counterexamples for request recovery and actual selected evidence."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import EvaluationResult
from darwinagent.engine import Pipeline
from darwinagent.experiments.stages import selected_stage
from darwinagent.runtime.execution import ExecutionSelection
from darwinagent.runtime.steps import StepJournal, UnknownRequest
from darwinagent.runtime.workspace import Workspace
from tests.support.clients import LedgerRecordedClient
from tests.support.device import case, client, spec


class ExecutionIntegrityTests(unittest.TestCase):
    def test_unknown_transport_is_not_terminal_answer_and_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            transport = client(c, answers=[OSError("response connection lost")])
            task = spec(root / "assets")
            with self.assertRaises(UnknownRequest):
                asyncio.run(
                    Pipeline(transport, root / "generation").run(
                        c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                    )
                )
            self.assertFalse(
                list((root / "generation" / c.id / "branches/main/answers").glob("*.json"))
            )
            fresh = client(c)
            with self.assertRaises(UnknownRequest):
                asyncio.run(
                    Pipeline(fresh, root / "generation").run(
                        c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                    )
                )
            self.assertEqual(fresh.calls, [])

    def test_database_submitted_file_prepared_does_not_create_new_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            journal = StepJournal(root / "steps", workspace=workspace)
            path, _ = journal.reserve("model", {"role": "answer"})
            original = json.loads(path.read_text())
            workspace.submit_request(original["request_id"])
            # Simulate a crash after the DB commit but before the journal file update.
            with self.assertRaises(UnknownRequest):
                StepJournal(root / "steps", workspace=workspace).reserve(
                    "model", {"role": "answer"}
                )
            self.assertEqual(json.loads(path.read_text())["request_id"], original["request_id"])

    def test_review_after_rerun_uses_latest_selected_answer_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c, config = case(), RunConfig(protocol_attempts=1)
            task = spec(root / "assets")
            asyncio.run(
                Pipeline(client(c), root / "generation").run(
                    c, task, config, execution=ExecutionSelection()
                )
            )
            replacement = "维护人是林，维护日期是2026-09-01。"
            rerun = asyncio.run(
                Pipeline(
                    client(
                        c,
                        answers=[
                            {"status": "answered", "answer": replacement, "node_ids": ["n000000"]}
                        ],
                    ),
                    root / "generation",
                ).run(
                    c,
                    task,
                    config,
                    execution=ExecutionSelection(mode="rerun", stages=("answer", "check")),
                )
            )
            self.assertEqual(rerun.answers[0].answer, replacement)
            transport = client(c)
            reviewed = asyncio.run(
                Pipeline(transport, root / "generation").run(
                    c, task, config, execution=ExecutionSelection(mode="rerun", stages=("review",))
                )
            )
            self.assertEqual(reviewed.answers[0].answer, replacement)
            self.assertEqual([row["role"] for row in transport.calls], ["review"])

    def test_changed_question_body_cannot_score_legacy_answer_with_same_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            asyncio.run(
                Pipeline(client(c), root / "B0/generation").run(
                    c, task, RunConfig(protocol_attempts=1)
                )
            )
            changed = replace(c, questions=(replace(c.questions[0], text="维护人是否是林？"),))
            calls = []

            class Evaluator:
                async def evaluate(self, result, asked=None):
                    calls.append(result)
                    return EvaluationResult({"score": 1}, 1, 1, 0, 0)

            with self.assertRaisesRegex(ValueError, "question|Question|version|Version"):
                asyncio.run(
                    selected_stage(
                        "B0",
                        (changed,),
                        task,
                        root=root,
                        client_factory=lambda _: LedgerRecordedClient({}),
                        evaluator_factory=lambda _client, _path: Evaluator(),
                        config=RunConfig(),
                        execution=ExecutionSelection(stages=("score",)),
                    )
                )
            self.assertEqual(calls, [])

    def test_unknown_closure_criterion_does_not_reuse_old_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            asyncio.run(
                Pipeline(client(c), root / "B0/generation").run(
                    c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            criterion = {"standard": 1}
            calls = []

            class Evaluator:
                async def evaluate(self, result, asked=None):
                    calls.append(criterion["standard"])
                    return EvaluationResult({"score": criterion["standard"]}, 1, 1, 0, 0)

            def factory(_client, _path):
                return Evaluator()

            def score():
                return asyncio.run(
                    selected_stage(
                        "B0",
                        (c,),
                        task,
                        root=root,
                        client_factory=lambda _: LedgerRecordedClient({}),
                        evaluator_factory=factory,
                        config=RunConfig(),
                        execution=ExecutionSelection(stages=("score",)),
                    )
                )[1]

            self.assertEqual(score().metrics["score"], 1)
            criterion["standard"] = 2
            self.assertEqual(score().metrics["score"], 2)
            self.assertEqual(calls, [1, 2])

    def test_concurrent_executor_does_not_submit_duplicate_work(self):
        from darwinagent.runtime.leases import TaskInProgress, execution_lease

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            with execution_lease(path):
                with self.assertRaises(TaskInProgress):
                    with execution_lease(path):
                        self.fail("second executor entered")
            with execution_lease(path):
                pass

    def test_score_reuses_success_per_question_and_changed_standard_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            q2 = replace(c.questions[0], id="q2")
            config = RunConfig(protocol_attempts=1)
            asyncio.run(
                Pipeline(client(c), root / "B0/generation").run(
                    c, task, config, execution=ExecutionSelection()
                )
            )
            version = {"id": "standard-v1"}
            calls = []

            class Evaluator:
                async def evaluate(self, result, asked=None):
                    calls.extend(a.question_id for a in result.answers)
                    return EvaluationResult({"score": 1}, 1, 1, 0, 0)

            def factory(_client, _path):
                return Evaluator()

            factory.criterion_id = lambda: version["id"]

            def score(input_case, mode="continue"):
                return asyncio.run(
                    selected_stage(
                        "B0",
                        (input_case,),
                        task,
                        root=root,
                        client_factory=lambda _: LedgerRecordedClient({}),
                        evaluator_factory=factory,
                        config=config,
                        execution=ExecutionSelection(mode=mode, stages=("score",)),
                    )
                )

            score(c)
            c2 = replace(c, questions=(c.questions[0], q2))
            asyncio.run(
                Pipeline(client(c), root / "B0/generation").run(
                    c2, task, config, execution=ExecutionSelection(question_ids=("q2",))
                )
            )
            score(c2)
            self.assertEqual(calls, ["q1", "q2"])
            version["id"] = "standard-v2"
            score(c2)
            self.assertEqual(calls, ["q1", "q2"])
            self.assertFalse(
                json.loads((root / "B0/stage.json").read_text())["comparison_reliable"]
            )
            score(c2, "rerun")
            self.assertEqual(calls, ["q1", "q2", "q1", "q2"])
            self.assertTrue(json.loads((root / "B0/stage.json").read_text())["comparison_reliable"])

    def test_failed_graph_preparation_retried_without_erasing_old_failure(self):
        from darwinagent.llm.recorded import RecordedClient

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            config = RunConfig(protocol_attempts=1)
            failed = asyncio.run(
                Pipeline(
                    RecordedClient({"extraction": [{"entities": [], "relations": []}]}),
                    root / "generation",
                ).run(c, task, config, execution=ExecutionSelection())
            )
            self.assertEqual(failed.answers[0].status, "execution_error")
            transport = client(c)
            recovered = asyncio.run(
                Pipeline(transport, root / "generation").run(
                    c, task, config, execution=ExecutionSelection(mode="retry_failed")
                )
            )
            self.assertEqual(recovered.answers[0].status, "answered")
            self.assertIn("extraction", [row["role"] for row in transport.calls])
            self.assertTrue(list((root / "generation").rglob("graph.failure.json")))

    def test_model_parameter_change_replays_saved_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = StepJournal(tmp)
            path, _ = journal.reserve(
                "model", {"role": "answer", "messages": [], "temperature": 0.2, "max_tokens": 100}
            )
            journal.submitted(path)
            journal.respond(path, {"content": "saved"})
            reply = StepJournal(tmp).reserve(
                "model", {"role": "answer", "messages": [], "temperature": 0, "max_tokens": 200}
            )[1]
            self.assertEqual(reply["content"], "saved")
