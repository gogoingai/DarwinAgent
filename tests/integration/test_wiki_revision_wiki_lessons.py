"""Offline regression scenarios for wiki lessons."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, QuestionInput
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.kernel import TaskSpec
from datasets.locomo.run import _trimmed_train_adapter
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK
from tests.support.graphs import corpus
from tests.support.recorded_wiki import WikiRecordedExperiment


class WikiEvidenceRegression(unittest.TestCase):
    def test_late_fault_survives_both_trace_budgets_and_is_runtime_experience(self):
        from darwinagent.experiments.wiki import _context_facts, bounded_trace

        trace = [{"stage": "tool", "asset_id": "f", "parameters": {"limit": i}} for i in range(9)]
        trace.append(
            {
                "stage": "tool_error",
                "asset_id": "f",
                "parameters": {"limit": 300},
                "error_type": "SandboxError",
                "error": "budget exhausted",
                "observation": {"steps_used": 30001},
            }
        )
        facts = {
            "candidate_version": "candidate",
            "training_examples": [
                {
                    "training_id": "conv::q",
                    "status": "execution_error",
                    "error": "budget exhausted",
                    "trace": bounded_trace(trace, 8),
                }
            ],
        }
        self.assertIn(
            "tool_error",
            [e["stage"] for e in _context_facts(facts)["training_examples"][0]["trace"]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            wiki = WikiMaintainer(
                tmp, "id", lambda _: self.fail("No extra model call"), RunConfig()
            )
            asyncio.run(
                wiki.record(
                    "R4",
                    "formal",
                    facts,
                    category="strategy",
                    scope="formal",
                    training_ids=["conv::q"],
                )
            )
            asyncio.run(
                wiki.record(
                    "R4",
                    "formal",
                    facts,
                    category="strategy",
                    scope="formal",
                    training_ids=["conv::q"],
                )
            )
            runtime = [e for e in wiki._wiki()["entries"] if e["category"] == "runtime"]
            self.assertEqual(len(runtime), 1)
            scenario = runtime[0]["facts"]["scenarios"][0]
            self.assertEqual(scenario["parameters"], {"limit": 300})
            self.assertEqual(scenario["steps_used"], 30001)
            self.assertEqual(runtime[0]["scope"], "formal")

    def test_decision_attribution_has_training_ids_code_and_generated_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(
                    runner.run(
                        runner.case.id,
                        TaskSpec.load(TASK / "task.yaml"),
                        rounds=2,
                        scope=("S", "F", "C", "P"),
                    )
                )
            requests = [
                json.loads(p.read_text())
                for p in (root / "optimization/maintenance").glob("*.request.json")
            ]
            decisions = [p for p in requests if p["event"]["kind"] == "decision"]
            self.assertEqual(len(decisions), 2)
            for payload in decisions:
                self.assertIn(
                    "MappingProxyType", payload["runtime_contract"]["input_freeze_implementation"]
                )
                self.assertTrue(payload["event"]["training_ids"])
                self.assertTrue(payload["event"]["facts"]["asset_changes"])
                example = payload["event"]["facts"]["training_examples"][0]
                self.assertIn(example["training_id"], payload["event"]["training_ids"])
                self.assertIn("generated_answer", example)
                self.assertIn("question", example)
                self.assertIn("source_text", example)
                self.assertNotIn("reference", example)
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            self.assertTrue(all(e["confidence"] == "hypothesis" for e in wiki["entries"]))

    def test_candidate_check_rejections_survive_compression_and_retain_check_identity(self):
        from darwinagent.experiments.wiki import _context_facts

        trace = [{"stage": "tool", "asset_id": "f"} for _ in range(10)]
        trace.append(
            {
                "stage": "candidate",
                "checks": [
                    {
                        "check_id": "c_shape",
                        "fingerprint": "check-hash",
                        "ok": False,
                        "issues": ["answer is not an array"],
                        "steps_used": 45,
                    }
                ],
            }
        )
        facts = {
            "training_examples": [
                {
                    "training_id": "trip::0",
                    "status": "execution_error",
                    "error": "ProtocolError",
                    "question_parameters": {"days": 3},
                    "trace": trace,
                }
            ]
        }
        self.assertIn(
            "candidate",
            [e["stage"] for e in _context_facts(facts)["training_examples"][0]["trace"]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            wiki = WikiMaintainer(tmp, "id", lambda _: self.fail("No model"), RunConfig())
            asyncio.run(wiki.record("B0", "formal", facts, category="strategy", scope="formal"))
            runtime = next(e for e in wiki._wiki()["entries"] if e["category"] == "runtime")
            row = runtime["facts"]["scenarios"][0]
            self.assertEqual(row["asset_id"], "c_shape")
            self.assertEqual(row["error_type"], "CandidateCheckRejected")
            self.assertEqual(row["parameters"], {"days": 3})

    def test_frozen_json_container_types_are_executable_and_documented(self):
        from darwinagent.experiments.bootstrap import _CORE_RULES
        from darwinagent.operators.sandbox import Interpreter, admit

        code = "def check(candidate):\n rows=candidate['structured_answer']\n issues=[]\n if not isinstance(rows,(list,tuple)):\n  issues.append('not array')\n for row in rows:\n  local=dict(row)\n  if not isinstance(local,dict):\n   issues.append('not object')\n return {'ok':not issues,'issues':issues}"
        snapshot = {"structured_answer": [{"days": 1}]}
        self.assertTrue(Interpreter(admit(code, "C"), {}).execute(snapshot)["ok"])
        bad = code.replace("isinstance(rows,(list,tuple))", "isinstance(rows,list)")
        self.assertFalse(Interpreter(admit(bad, "C"), {}).execute(snapshot)["ok"])
        self.assertIn("input JSON arrays as tuples", _CORE_RULES)
        self.assertIn("C MUST NOT set role; P MUST NOT set stage", _CORE_RULES)
        self.assertNotIn("F and C MUST NOT set role or stage", _CORE_RULES)

    def test_failed_candidate_payload_type_is_evidence_not_a_guessed_cause(self):
        from darwinagent.contracts import AnswerResult, RunResult
        from darwinagent.experiments.feedback import _wiki_training_evidence

        check = {"check_id": "c_shape", "ok": False, "issues": ["not array"]}
        answer = AnswerResult(
            "q1",
            "execution_error",
            "",
            error="ProtocolError",
            trace=(
                {
                    "stage": "candidate",
                    "candidate": {"status": "answered", "answer": '[{"days":1}]', "node_ids": []},
                    "checks": [check],
                },
            ),
        )
        result = RunResult("conv-x", "identity", "assets", (answer,), 0)
        case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "plan", {"days": 1}),))
        facts = _wiki_training_evidence([case], [result])
        event = facts["training_examples"][0]["trace"][0]
        self.assertEqual(event["candidate_summary"]["json_type"], "list")
        self.assertEqual(event["candidate_summary"]["answer"], '[{"days":1}]')
        self.assertEqual(event["checks"], [check])
        self.assertNotIn("gold", json.dumps(facts))

    def test_new_error_pattern_triggers_maintenance_without_repeating_known_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def factory(_):
                calls.append(True)
                return LedgerRecordedClient(
                    {
                        "wiki_maintainer": [
                            {
                                "cause": "可验证的运行假设",
                                "action": "按契约修复",
                                "training_ids": [],
                            }
                        ]
                    }
                )

            wiki = WikiMaintainer(tmp, "id", factory, RunConfig(protocol_attempts=1))
            first = {
                "status": "failed",
                "admission": {
                    "scenarios": [
                        {
                            "asset_id": "f",
                            "scenario_id": "base",
                            "status": "failed",
                            "required": True,
                            "error_type": "ValueError",
                            "error": "bad date",
                        }
                    ]
                },
            }
            second = {
                "status": "failed",
                "admission": {
                    "scenarios": [
                        {
                            "asset_id": "f",
                            "scenario_id": "stress",
                            "status": "failed",
                            "required": True,
                            "error_type": "SandboxError",
                            "error": "budget exhausted",
                        }
                    ]
                },
            }
            self.assertTrue(wiki.new_failure(first))
            asyncio.run(wiki.record("R1", "attempt", first, infer=wiki.new_failure(first)))
            self.assertFalse(wiki.new_failure(first))
            self.assertTrue(wiki.new_failure(second))
            asyncio.run(wiki.record("R1", "attempt", second, infer=wiki.new_failure(second)))
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(wiki.context()["lessons"]), 2)

    def test_fixed_ten_question_selection_does_not_change_holdout(self):
        questions = tuple(QuestionInput(str(i), f"问题{i}") for i in range(25))

        class Adapter:
            def generation_input(self, case_id):
                return CaseInput(case_id, corpus(), questions)

        selected = tuple(str(i) for i in range(0, 20, 2))
        adapter = _trimmed_train_adapter(Adapter(), ("train",), 10, selected)
        self.assertEqual(tuple(q.id for q in adapter.generation_input("train").questions), selected)
        self.assertEqual(len(adapter.generation_input("test").questions), 25)
        with self.assertRaises(ValueError):
            _trimmed_train_adapter(Adapter(), ("train",), 10, ("1", "1"))
