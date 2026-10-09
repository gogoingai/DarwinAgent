"""Reproduce contradictory reviews through the real agents and stage checkpoints."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import EvaluationResult
from darwinagent.experiments.policy import AdoptionPolicy
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.runtime.artifacts import digest
from tests.support.clients import LedgerRecordedClient
from tests.support.device import case, extraction, review, spec


class ReviewExhaustionRecoveryTests(unittest.TestCase):
    def test_only_failed_question_retries_with_history_and_fresh_responses(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task = spec(root / "assets")
            c = case()
            c = replace(c, questions=(c.questions[0], replace(c.questions[0], id="healthy")))
            call = {"action": "call", "asset_id": "device_lookup", "parameters": {"serial": "D-17"}}
            ready = {"action": "ready"}
            answer = {
                "status": "answered",
                "answer": "林于2026-09-01维护。",
                "node_ids": ["n000000"],
            }
            first, second = review(False), review(False)
            first["feedback"] = "删除学生身份，没有来源支持。"
            second["feedback"] = "必须补上学生身份。"
            transport = LedgerRecordedClient(
                {
                    "extraction": [extraction(c)],
                    "tools": [call, ready, call, ready, call, ready, call, ready],
                    "answer": [answer] * 4,
                    "review": [first, second, review(), review()],
                }
            )

            class Evaluator:
                async def evaluate(self, result, asked=None):
                    faults = [a for a in result.answers if a.status == "execution_error"]
                    return EvaluationResult(
                        {"m": 2 - len(faults)}, 2, 2 - len(faults), len(faults), 0
                    )

            runner = ExperimentRunner(
                None,
                lambda *_: Evaluator(),
                None,
                RunConfig(answer_attempts=2, protocol_attempts=1, concurrency=1, tool_steps=2),
                None,
                root,
                client_factory=lambda _: transport,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                results, scores = asyncio.run(runner._stage("B0", [c], task))
            self.assertEqual(scores.completed, 2)
            self.assertEqual(scores.generation_faults, 0)
            self.assertTrue(all(a.status == "answered" for a in results[0].answers))
            summary = json.loads((root / "B0/stage.json").read_text())
            retry = summary["fault_retries"][c.id]
            self.assertEqual((retry["retried"], retry["recovered"]), (1, 1))
            self.assertEqual(retry["fault_categories"], {"review_exhausted": 1})
            answers_dir = root / "B0/generation" / c.id / "answers"
            archives = list((answers_dir / "history" / digest("q1")).glob("*.json"))
            self.assertEqual(len(archives), 1)
            original = json.loads(archives[0].read_text())["result"]
            self.assertTrue(original["error"].startswith("FeedbackExhausted:"))
            self.assertEqual(len([e for e in original["trace"] if e["stage"] == "review"]), 2)
            self.assertFalse((answers_dir / "history" / digest("healthy")).exists())
            requests = [r for r in transport.calls if r["role"] == "answer"]
            self.assertEqual(len(requests), 4)
            self.assertFalse(requests[-1]["use_cache"])
            self.assertEqual(
                len(json.loads(requests[-1]["messages"][-1]["content"])["feedback"]), 2
            )
            reviews = [r for r in transport.calls if r["role"] == "review"]
            history = json.loads(reviews[1]["messages"][-1]["content"])["revision_history"]
            self.assertEqual(history[0]["review"]["feedback"], first["feedback"])
            before = len(transport.calls)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(runner._stage("B0", [c], task))
            self.assertEqual(len(transport.calls), before, "resume must reuse the recovered view")
            resumed = json.loads((root / "B0/stage.json").read_text())
            self.assertEqual(resumed["fault_retries"][c.id]["recovered"], 1)

    def test_persistent_review_failure_keeps_healthy_progress_and_bounded_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task, c = spec(root / "assets"), case()
            c = replace(c, questions=(c.questions[0], replace(c.questions[0], id="healthy")))
            call = {"action": "call", "asset_id": "device_lookup", "parameters": {"serial": "D-17"}}
            answer = {
                "status": "answered",
                "answer": "林于2026-09-01维护。",
                "node_ids": ["n000000"],
            }
            transport = LedgerRecordedClient(
                {
                    "extraction": [extraction(c)],
                    "tools": [call, {"action": "ready"}] * 3,
                    "answer": [answer] * 3,
                    "review": [review(False), review(), review(False)],
                }
            )

            class Evaluator:
                async def evaluate(self, result, asked=None):
                    faults = [a for a in result.answers if a.status == "execution_error"]
                    return EvaluationResult(
                        {"m": 2 - len(faults)}, 2, 2 - len(faults), len(faults), 0
                    )

            runner = ExperimentRunner(
                None,
                lambda *_: Evaluator(),
                None,
                RunConfig(answer_attempts=1, protocol_attempts=1, concurrency=1, tool_steps=2),
                None,
                root,
                client_factory=lambda _: transport,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                results, scores = asyncio.run(runner._stage("B0", [c], task))
            self.assertEqual(scores.generation_faults, 1)
            self.assertEqual(results[0].answers[1].status, "answered")
            self.assertEqual(results[0].answers[0].status, "execution_error")
            healthy = root / "B0/generation" / c.id / "answers" / (digest("healthy") + ".json")
            saved_bytes = healthy.read_bytes()
            calls = len(transport.calls)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(runner._stage("B0", [c], task))
            self.assertEqual(len(transport.calls), calls)
            self.assertEqual(healthy.read_bytes(), saved_bytes)
            journal = json.loads((root / "B0/fault-retry" / (c.id + ".json")).read_text())
            self.assertEqual(journal["questions"]["q1"]["attempts"], 1)
            self.assertEqual(journal["questions"]["q1"]["final_error_type"], "FeedbackExhausted")

    def test_exhaustion_stays_faulted_and_does_not_retry_forever(self):
        from darwinagent.contracts import AnswerResult
        from darwinagent.experiments.recovery import _retryable_answer, answer_fault_category

        old = AnswerResult(
            "q",
            "execution_error",
            "",
            error="ProtocolError: Feedback retries exhausted without a publishable candidate",
            trace=({"stage": "review", "accepted": False, "feedback": "no proof"},),
        )
        self.assertEqual(answer_fault_category(old), "review_exhausted")
        self.assertTrue(_retryable_answer(old))
        malformed = replace(old, error="ProtocolError: Expected JSON", trace=())
        self.assertEqual(answer_fault_category(malformed), "protocol_error")
        self.assertFalse(_retryable_answer(malformed))
        tool = replace(old, trace=old.trace + ({"stage": "tool_error"},))
        self.assertEqual(answer_fault_category(tool), "deterministic_tool_error")
        self.assertFalse(_retryable_answer(tool))

    def test_incomplete_baseline_does_not_establish_quality_improvement(self):
        policy = AdoptionPolicy("m", ())
        baseline = EvaluationResult({"m": 0}, 2, 1, 1, 0)
        candidate = EvaluationResult({"m": 1}, 2, 2, 0, 0)
        decision = policy.decide(baseline, candidate)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["comparison_status"], "incomplete_baseline")
        self.assertFalse(decision["quality_improvement_established"])

    def test_wiki_and_proposer_get_review_failure_signal(self):
        from darwinagent.experiments.graph_evidence import asset_change_signals
        from darwinagent.experiments.wiki_evidence import safe_feedback

        failure = {
            "case_id": "c",
            "question_id": "q",
            "error": "FeedbackExhausted: no candidate",
            "fault_category": "review_exhausted",
        }
        feedback = {
            "scores": EvaluationResult({"m": 0}, 1, 0, 1, 0).to_dict(),
            "generation_failures": [failure],
        }
        self.assertEqual(
            safe_feedback(feedback)["generation_failures"][0]["fault_category"], "review_exhausted"
        )
        signals = asset_change_signals(feedback)
        self.assertEqual(signals[0]["asset_kinds"], ["P"])
        self.assertIn("P.review", signals[0]["next_check"])
