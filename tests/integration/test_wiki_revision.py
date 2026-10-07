"""Regression cases for interface-independent admission and evidence-based Wiki learning."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, GraphResult, QuestionInput
from darwinagent.experiments.admission import (
    AdmissionError,
    _pressure_graph,
    _traversal_shape,
    admit_candidate,
)
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.functions import FunctionRegistry
from datasets.locomo.run import _trimmed_train_adapter
from tests.fixtures import TASK
from tests.integration.test_agentic_round import cold_bundle, corpus, gvtest_graph
from tests.integration.test_experiment import LedgerRecordedClient
from tests.integration.test_wiki_optimization import WikiRecordedExperiment


class InterfaceAdmissionRegression(unittest.TestCase):
    def graph(self):
        return GraphResult(gvtest_graph(), {b.source.id: b for b in corpus()})

    def bundle(self, root, asset):
        seed = cold_bundle(root / "seed", with_c=False)
        return KernelAssets(
            tuple(a for a in seed.assets.assets if a.kind != "F") + (asset,)
        ).export(root / "candidate")

    def admit(self, bundle, graph, path, config=None):
        case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "有哪些事实？"),))
        return admit_candidate(bundle, [case], {"conv-x": graph}, config or RunConfig(), (), path)

    def test_seed_direction_aliases_receive_identical_required_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = []
            outputs = []
            for index, (seed, direction) in enumerate(
                (("node_id", "direction"), ("seed_id", "orientation"), ("种子", "方向"))
            ):
                root = Path(tmp) / str(index)
                f = Asset(
                    "f_expand",
                    "F",
                    f"def run(params):\n return traverse(params['{seed}'],'归属于',params['{direction}'])",
                    {
                        "type": "object",
                        "properties": {
                            seed: {"type": "string"},
                            direction: {"type": "string", "enum": ["in", "out"]},
                        },
                        "required": [seed, direction],
                    },
                    {"type": "array"},
                    ["schema"],
                    trial_inputs=({seed: "n000000", direction: "out"},),
                )
                bundle = self.bundle(root, f)
                graph = self.graph()
                outputs.append(
                    FunctionRegistry(bundle).call(f.id, {seed: "n000000", direction: "out"}, graph)[
                        "node_ids"
                    ]
                )
                reports.append(self.admit(bundle, graph, root / "admission.json"))
            self.assertEqual(outputs, [["n000002"]] * 3)
            for report in reports:
                self.assertEqual(report["verdict"], "passed")
                for direction in ("in", "out"):
                    self.assertTrue(
                        any(
                            s["scenario_id"] == "high_degree_" + direction
                            and s["required"]
                            and s["status"] == "passed"
                            for s in report["scenarios"]
                        )
                    )

    def test_internal_seed_uses_copy_and_reaches_high_degree_before_limit(self):
        graph = self.graph()
        g = graph.graph.copy()
        nodes = list(g.nodes)
        g.add_edge(nodes[1], nodes[2], relation="归属于")
        graph = replace(graph, graph=g)
        f = Asset(
            "f_internal",
            "F",
            "def run(params):\n rows=nodes(entity_type='原子事实',limit=1)\n return traverse(rows[0]['node_id'],'归属于',params['方向'])",
            {
                "type": "object",
                "properties": {"方向": {"type": "string", "enum": ["out"]}},
                "required": ["方向"],
            },
            {"type": "array"},
            ["schema"],
            trial_inputs=({"方向": "out"},),
        )
        original = list(g.nodes)
        pressure = _pressure_graph(f, {"方向": "out"}, graph, "high_degree_out")
        self.assertEqual(list(g.nodes), original)
        self.assertEqual(set(g.nodes), set(pressure.graph.nodes))
        self.assertEqual(set(g.edges(keys=True)), set(pressure.graph.edges(keys=True)))
        self.assertEqual(list(pressure.graph.nodes)[0], nodes[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root, f)
            report = self.admit(bundle, graph, root / "admission.json")
            stress = next(s for s in report["scenarios"] if s["scenario_id"] == "high_degree_out")
            self.assertEqual(stress["pressure_kind"], "graph_row_order")
            self.assertEqual(stress["traverse_observations"][0]["matched_edges"], 2)
            self.assertEqual(report["verdict"], "passed")

    def test_one_empty_selector_does_not_invalidate_a_covered_pressure_path(self):
        f = Asset(
            "f_internal",
            "F",
            "def run(params):\n if params['mode']=='empty':\n  return []\n rows=nodes(entity_type='原子事实',limit=1)\n return traverse(rows[0]['node_id'],'归属于')",
            {"type": "object", "properties": {"mode": {"type": "string"}}, "required": ["mode"]},
            {"type": "array"},
            ["schema"],
            trial_inputs=({"mode": "empty"}, {"mode": "actual"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root, f)
            report = self.admit(bundle, self.graph(), root / "admission.json")
            self.assertEqual(report["verdict"], "passed")
            trials = [s for s in report["scenarios"] if s["scenario_id"] == "high_degree_out"]
            self.assertTrue(any(s["status"] == "passed" and s["required"] for s in trials))
            self.assertTrue(
                any(s["error_type"] == "CoverageGap" and not s["required"] for s in trials)
            )

    def test_internal_seed_resource_failure_remains_rejected(self):
        g = gvtest_graph()
        nodes = list(g.nodes)
        for i in range(80):
            nid = f"heavy-{i}"
            g.add_node(nid, etype="人物", __key__="{}", __sources__=["conv-x"], 姓名=str(i))
            g.add_edge(nodes[1], nid, relation="归属于")
        graph = GraphResult(g, {b.source.id: b for b in corpus()})
        f = Asset(
            "f_internal",
            "F",
            "def run(params):\n seeds=nodes(entity_type='原子事实',limit=1)\n rows=traverse(seeds[0]['node_id'],'归属于')\n out=[]\n for row in rows:\n  out.append(row)\n return out",
            {"type": "object", "properties": {"mode": {"type": "string"}}},
            {"type": "array"},
            ["schema"],
            trial_inputs=({"mode": "all"},),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root, f)
            with self.assertRaises(AdmissionError) as caught:
                self.admit(bundle, graph, root / "admission.json", RunConfig(function_steps=100))
            report = caught.exception.report
            self.assertTrue(
                any(
                    s["scenario_id"] == "high_degree_out"
                    and s["status"] == "failed"
                    and s["error_type"] == "SandboxError"
                    for s in report["scenarios"]
                )
            )


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
        from darwinagent.experiments.runner import _wiki_training_evidence

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


class BootstrapFeedbackRegression(unittest.TestCase):
    def test_unsupported_union_types_are_reported_together_before_data_execution(self):
        from darwinagent.kernel.validation import validate_bundle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = cold_bundle(root / "seed", with_c=False)
            f = Asset(
                "f_bad_contract",
                "F",
                "def run(params):\n return []",
                {"type": "object", "properties": {}},
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": ["string", "null"]},
                            "日期": {"type": ["string", "null"]},
                        },
                        "additionalProperties": True,
                    },
                },
                ["schema"],
                trial_inputs=({},),
            )
            bundle = KernelAssets(
                tuple(a for a in seed.assets.assets if a.kind != "F") + (f,)
            ).export(root / "candidate")
            with self.assertRaises(ValueError) as caught:
                validate_bundle(bundle)
            self.assertIn("node_id", str(caught.exception))
            self.assertIn("日期", str(caught.exception))
            self.assertIn("not a union/list", str(caught.exception))

    def test_resource_feedback_contains_real_input_steps_and_matched_edges(self):
        from darwinagent.experiments.bootstrap import _trial_failure_feedback

        report = {
            "scenarios": [
                {
                    "asset_id": "f_expand",
                    "scenario_id": "high_degree_in",
                    "status": "failed",
                    "required": True,
                    "error_type": "SandboxError",
                    "error": "Restricted execution budget exhausted",
                    "parameters": {"node_ids": ["n000114"]},
                    "steps_used": 30001,
                    "step_budget": 30000,
                    "traverse_observations": [
                        {"direction": "in", "relation": "属于主题", "matched_edges": 145}
                    ],
                }
            ]
        }
        feedback = _trial_failure_feedback(report)
        for value in ("n000114", "30001", "30000", "145", "昂贵处理结束后才截断"):
            self.assertIn(value, feedback)


class SelectorSliceRegression(unittest.TestCase):
    def test_row_slice_limit_is_not_external_seed(self):
        asset = Asset(
            "f",
            "F",
            "def run(params):\n rows=nodes(limit=50)\n bounded=rows[:params['limit']]\n ids=[]\n for r in bounded:\n  ids.append(r['node_id'])\n return traverse(ids,'related')",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
            {"type": "array"},
            ["schema"],
            trial_inputs=({"limit": 3},),
        )
        self.assertEqual(_traversal_shape(asset)[0], set())

    def test_external_seed_slice_preserves_only_identity_parameter(self):
        asset = Asset(
            "f",
            "F",
            "def run(params):\n ids=params['seed_ids'][:params['limit']]\n return traverse(ids,'related')",
            {
                "type": "object",
                "properties": {
                    "seed_ids": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
            },
            {"type": "array"},
            ["schema"],
            trial_inputs=({"seed_ids": ["n000001"], "limit": 1},),
        )
        self.assertEqual(_traversal_shape(asset)[0], {"seed_ids"})


class CurrentAssetReferenceRegression(unittest.TestCase):
    def payload(self, base):
        asset = next(a for a in base.assets.assets if a.kind == "P")
        return {
            "patches": [
                {
                    "asset": asset.to_dict(),
                    "base_fingerprint": "current:" + asset.id,
                    "reason": "Verified current base reference",
                    "training_evidence": ["6:conv-x::q1"],
                }
            ]
        }

    def test_reference_resolves_exact_current_fingerprint_and_saved_reply(self):
        from darwinagent.experiments.proposal import ProposalGenerator
        from darwinagent.experiments.runner import ExperimentRunner

        with tempfile.TemporaryDirectory() as tmp:
            base = cold_bundle(Path(tmp) / "base", with_c=False)
            obj = self.payload(base)
            patch = ProposalGenerator.decode(obj, base)[0]
            original = next(a for a in base.assets.assets if a.id == patch.asset.id)
            self.assertEqual(patch.base_fingerprint, original.fingerprint)
            self.assertTrue(ExperimentRunner._valid_proposal_raw(json.dumps(obj), base))
            self.assertFalse(ExperimentRunner._valid_proposal_raw(json.dumps(obj)))

    def test_reference_cannot_select_other_asset_or_change_kind(self):
        from darwinagent.experiments.proposal import ProposalGenerator

        with tempfile.TemporaryDirectory() as tmp:
            base = cold_bundle(Path(tmp) / "base", with_c=False)
            obj = self.payload(base)
            obj["patches"][0]["base_fingerprint"] = "current:unknown"
            with self.assertRaisesRegex(ValueError, "reference"):
                ProposalGenerator.decode(obj, base)
            obj = self.payload(base)
            obj["patches"][0]["asset"]["kind"] = "F"
            obj["patches"][0]["asset"]["role"] = ""
            obj["patches"][0]["asset"]["trial_inputs"] = [{}]
            with self.assertRaisesRegex(ValueError, "type change"):
                ProposalGenerator.decode(obj, base)

    def test_literal_wrong_hash_is_rejected_and_legacy_literal_remains_valid(self):
        from darwinagent.experiments.proposal import ProposalGenerator

        with tempfile.TemporaryDirectory() as tmp:
            base = cold_bundle(Path(tmp) / "base", with_c=False)
            obj = self.payload(base)
            obj["patches"][0]["base_fingerprint"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "Wrong fingerprint"):
                ProposalGenerator.decode(obj, base)
            asset = next(a for a in base.assets.assets if a.id == obj["patches"][0]["asset"]["id"])
            obj["patches"][0]["base_fingerprint"] = asset.fingerprint
            self.assertEqual(ProposalGenerator.decode(obj)[0].base_fingerprint, asset.fingerprint)


class EvaluatorInterfaceRegression(unittest.TestCase):
    def test_generic_non_numeric_question_and_opt_in_subset(self):
        from darwinagent.experiments.runner import _evaluate_stage

        result = object()
        questions = (QuestionInput("trip-A", "where?"),)
        seen = []

        class GenericEvaluator:
            async def evaluate(self, value):
                seen.append(value)
                return "generic"

        class SubsetEvaluator:
            async def evaluate(self, value, *, asked):
                seen.append((value, asked))
                return "subset"

        self.assertEqual(
            asyncio.run(_evaluate_stage(GenericEvaluator(), result, questions)), "generic"
        )
        self.assertEqual(
            asyncio.run(_evaluate_stage(SubsetEvaluator(), result, questions)), "subset"
        )
        self.assertEqual(seen, [result, (result, ("trip-A",))])
