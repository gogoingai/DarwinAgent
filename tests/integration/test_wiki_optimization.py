"""Wiki training memory through real recorded bootstrap, proposal and scoring."""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from darwinagent.config import RunConfig
from darwinagent.experiments.wiki import WikiMaintainer, safe_feedback
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.runtime.artifacts import digest
from darwinagent.experiments.proposal import ProposalGenerator
from darwinagent.experiments.bootstrap import AssetBootstrapper, _trial_failure_feedback
from darwinagent.agents.protocol import ProtocolError
from darwinagent.kernel.revision import AssetPatch, AssetRevisionService, training_id
from tests.fixtures import TASK, client
from tests.integration.test_experiment import LedgerRecordedClient, RecordedExperiment


class WikiRecordedExperiment(RecordedExperiment):
    def __init__(self, root):
        super().__init__(root)
        self.optimization_mode = "wiki"

    def _client(self, stage):
        if stage == "optimization":
            return LedgerRecordedClient({"wiki_maintainer": [
                {"cause": "需按来源复检", "action": "核对训练轨迹", "training_ids": []}]})
        status = self.root / stage / "optimization/attempt-0/status.json"
        if (stage.startswith("R") and status.exists()
                and json.loads(status.read_text())["state"] == "passed"
                and not self.stage_clients[stage]):
            self.stage_clients[stage] += 1
            return LedgerRecordedClient(client(self.case).replies)
        return super()._client(stage)


class WikiOptimizationTests(unittest.TestCase):
    def test_wiki_drives_next_proposal_and_retains_rejected_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(runner.run(
                    runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=2,
                    scope=("S", "F", "C", "P")))
            self.assertEqual([d["accepted"] for d in summary["rounds"]], [True, False])
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            self.assertIn("decision", [e["kind"] for e in wiki["entries"]])
            rejected = next(e for e in wiki["entries"]
                            if e["stage"] == "R2" and e["kind"] == "decision")
            self.assertFalse(rejected["facts"]["accepted"])
            self.assertEqual(rejected["facts"]["candidate"]["metrics"]["precise"], 1)
            second = json.loads((root / "R2/optimization/attempt-0/proposal-call.json").read_text())
            self.assertIn("wiki", second["input"])
            self.assertNotIn("task_training_feedback", second["input"])
            self.assertNotIn("previous_admission_error", second["input"])
            self.assertGreater(second["input"]["wiki"]["version"], 0)
            self.assertTrue((root / "R1/optimization/attempt-0/status.json").exists())

    def test_ten_recorded_rounds_keep_rejected_experience_without_candidate_carryover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)
            with contextlib.redirect_stdout(io.StringIO()):
                result = asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"),
                                                rounds=10, scope=("S", "F", "C", "P")))
            self.assertEqual(len(result["rounds"]), 10)
            self.assertEqual(sum(d["accepted"] for d in result["rounds"]), 1)
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            input_10 = json.loads((root / "R10/optimization/attempt-0/proposal-call.json").read_text())
            self.assertTrue(any(e["stage"] == "R9" and e["kind"] == "decision"
                                for e in input_10["input"]["wiki"]["entries"]))
            self.assertEqual(input_10["input"]["base_version"],
                             result["rounds"][0]["candidate_version"])
            self.assertEqual(len(wiki["consumed_ids"]), len(set(wiki["consumed_ids"])))
            resumed = WikiRecordedExperiment(root)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(resumed.run(resumed.case.id, TaskSpec.load(TASK / "task.yaml"),
                                        rounds=10, resume=True, scope=("S", "F", "C", "P")))
            after = json.loads((root / "optimization/wiki.json").read_text())
            self.assertEqual(wiki, after)

    def test_sanitized_grades_exclude_answer_derived_fields(self):
        raw = {"scores": {"metrics": {"precise": 0}, "total": 1, "completed": 1,
                          "diagnostics": [{"gold": "secret"}]},
               "diagnostics": [{"case_id": "train", "diagnostic": {
                   "question_id": "q", "precise": False, "lenient": False,
                   "answer": "secret", "reference": "secret",
                   "missing_elements": ["secret"], "wrong_elements": ["secret"]}}]}
        self.assertNotIn("secret", json.dumps(safe_feedback(raw)))

    def test_maintenance_failure_and_recovery_do_not_duplicate_facts_or_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            def client(_stage):
                return LedgerRecordedClient({"wiki_maintainer": [RuntimeError("offline")]})
            wiki = WikiMaintainer(tmp, "identity", client, RunConfig(protocol_attempts=2), limit=2)
            asyncio.run(wiki.record("B0", "formal", {"scores": {"total": 1}}, infer=True))
            before = json.loads(wiki.wiki_path.read_text())
            self.assertTrue(before["entries"][0]["pending_attribution"])
            self.assertEqual(json.loads(wiki.state_path.read_text())["reserved_calls"], 1)
            wiki.reconcile()
            asyncio.run(wiki.record("B0", "formal", {"scores": {"total": 1}}, infer=True))
            self.assertEqual(before, json.loads(wiki.wiki_path.read_text()))
            self.assertEqual(json.loads(wiki.state_path.read_text())["reserved_calls"], 1)

    def test_resume_after_decision_persisted_only_backfills_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)
            original = WikiMaintainer.record

            async def interrupt(self, stage, kind, facts, **kwargs):
                if stage == "R1" and kind == "decision":
                    raise RuntimeError("interrupt after decision")
                return await original(self, stage, kind, facts, **kwargs)

            with mock.patch.object(WikiMaintainer, "record", interrupt):
                with self.assertRaisesRegex(RuntimeError, "interrupt after decision"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"),
                                               rounds=1, scope=("S", "F", "C", "P")))
            self.assertTrue((root / "R1/decision.json").exists())
            resumed = WikiRecordedExperiment(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(resumed.run(resumed.case.id, TaskSpec.load(TASK / "task.yaml"),
                                                 rounds=1, resume=True, scope=("S", "F", "C", "P")))
            self.assertEqual(len(summary["rounds"]), 1)
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            self.assertEqual(sum(e["stage"] == "R1" and e["kind"] == "decision"
                                 for e in wiki["entries"]), 1)

    def test_resume_after_proposal_and_candidate_export_reuses_saved_model_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)
            original = ExperimentRunner._preflight

            def interrupt(self, candidate, *args, **kwargs):
                if ".candidate-attempt-" in str(candidate.root):
                    raise RuntimeError("interrupt after candidate export")
                return original(self, candidate, *args, **kwargs)

            with mock.patch.object(ExperimentRunner, "_preflight", interrupt):
                with self.assertRaisesRegex(RuntimeError, "interrupt after candidate export"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"),
                                               rounds=1, scope=("S", "F", "C", "P")))
            proposal = root / "R1/optimization/attempt-0/proposal-call.json"
            self.assertTrue(proposal.exists())
            self.assertTrue((root / "R1/.candidate-attempt-0/bundle/manifest.json").exists())

            resumed = WikiRecordedExperiment(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(resumed.run(resumed.case.id, TaskSpec.load(TASK / "task.yaml"),
                                                 rounds=1, resume=True, scope=("S", "F", "C", "P")))
            self.assertTrue(summary["rounds"][0]["accepted"])
            self.assertFalse(any(call["role"] == "proposal" for transport in resumed.created
                                 for call in transport.calls))
            self.assertTrue(proposal.exists())

    def test_failed_attempt_is_available_to_next_proposal_only_through_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            class RetryRecorded(WikiRecordedExperiment):
                def _client(self, stage):
                    first = self.root / "R1/optimization/attempt-0/status.json"
                    second = self.root / "R1/optimization/attempt-1/status.json"
                    if (stage == "R1" and self.stage_clients[stage] == 1
                            and first.exists() and
                            json.loads(first.read_text())["state"] == "failed"
                            and (not second.exists() or
                                 json.loads(second.read_text())["state"] == "reserved")):
                        self.stage_clients[stage] = 0
                    return super()._client(stage)
            runner = RetryRecorded(root)
            original = ProposalGenerator.propose

            async def fail_first(self, *args, **kwargs):
                if "attempt-0" in str(args[5]):
                    raise ValueError("static candidate shape invalid")
                return await original(self, *args, **kwargs)

            with mock.patch.object(ProposalGenerator, "propose", fail_first):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = asyncio.run(runner.run(
                        runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=1,
                        scope=("S", "F", "C", "P")))
            self.assertTrue(result["rounds"][0]["accepted"])
            first = json.loads((root / "R1/optimization/attempt-0/status.json").read_text())
            second = json.loads((root / "R1/optimization/attempt-1/proposal-call.json").read_text())
            self.assertEqual(first["state"], "failed")
            self.assertTrue(any("static candidate shape invalid" in str(e["facts"])
                                for e in second["input"]["wiki"]["entries"]))
            self.assertNotIn("previous_admission_error", second["input"])

    def test_saved_report_requires_matching_identity_and_separate_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = WikiRecordedExperiment(Path(tmp))
            bundle = load_assets(TASK).export(Path(tmp) / "bundle")
            report = {"verdict": "passed", "candidate_version": bundle.version,
                      "config_digest": digest(runner.config.to_dict()),
                      "asset_fingerprints": {a.id: a.fingerprint for a in bundle.assets.assets}}
            self.assertTrue(runner._wiki_report_valid(report, bundle))
            self.assertFalse(runner._wiki_report_valid(report, bundle, smoke=True))
            self.assertTrue(runner._wiki_report_valid({**report, "smoke": {"status": "passed"}},
                                                      bundle, smoke=True))
            self.assertFalse(runner._wiki_report_valid({**report, "config_digest": "stale"},
                                                       bundle))
            self.assertFalse(runner._wiki_report_valid({**report, "verdict": "timeout"},
                                                       bundle))

    def test_failed_b0_persists_protocol_evidence_and_uses_ten_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            class BrokenBootstrap(WikiRecordedExperiment):
                def _client(self, stage):
                    if stage == "B0":
                        return LedgerRecordedClient({"bootstrap": ["not json"] * 10})
                    return super()._client(stage)
            runner = BrokenBootstrap(root)
            with self.assertRaisesRegex(Exception, "JSONDecodeError"):
                asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"),
                                       rounds=0, scope=("S", "F", "C", "P")))
            call = json.loads((root / "B0/bootstrap-call.json").read_text())
            self.assertEqual(len(call["raw_outputs"]), 10)
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            self.assertEqual([e["kind"] for e in wiki["entries"]], ["bootstrap_failure"])
            self.assertIn("JSONDecodeError", wiki["entries"][0]["facts"]["error"])

    def test_failed_b0_consumes_saved_trial_before_terminal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)

            async def failed(_self, *args, trial_record=None, **kwargs):
                trial_record({"verdict": "failed", "candidate_version": "rejected",
                              "scenarios": [{"asset_id": "F", "status": "failed"}]})
                raise ProtocolError("bootstrap attempts exhausted")

            with mock.patch.object(AssetBootstrapper, "initialize", failed):
                with self.assertRaises(ProtocolError):
                    asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"),
                                           rounds=0, scope=("S", "F", "C", "P")))
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            self.assertEqual([e["kind"] for e in wiki["entries"]],
                             ["bootstrap_trial", "bootstrap_failure"])
            self.assertEqual(wiki["entries"][0]["facts"]["verdict"], "failed")

    def test_maintenance_cap_keeps_fact_recording_after_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            def client(_stage):
                return LedgerRecordedClient({"wiki_maintainer": [
                    {"cause": "unknown", "action": "investigate", "training_ids": []}]})
            wiki = WikiMaintainer(tmp, "identity", client, RunConfig(protocol_attempts=1), limit=3)
            for n in range(6):
                asyncio.run(wiki.record("R1", "decision", {"attempt": n}, infer=True))
            entries = json.loads(wiki.wiki_path.read_text())["entries"]
            self.assertEqual(len(entries), 6)
            self.assertEqual(sum(e["pending_attribution"] for e in entries), 3)
            self.assertEqual(json.loads(wiki.state_path.read_text())["reserved_calls"], 3)

    def test_same_attempt_accepts_joint_s_f_c_p_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = load_assets(TASK).export(Path(tmp) / "base")
            tid = training_id("train", "q1")
            by_kind = {kind: next(a for a in base.assets.assets if a.kind == kind)
                       for kind in ("S", "F", "C", "P")}
            patches = tuple(AssetPatch(replace(asset, description=asset.description + " reviewed"),
                                       asset.fingerprint, "validated current training issue", (tid,))
                            for asset in by_kind.values())
            candidate = AssetRevisionService().propose(
                base, patches, Path(tmp) / "attempt", (tid,),
                allowed_kinds=("S", "F", "C", "P"))
            self.assertNotEqual(candidate.version, base.version)
            self.assertEqual({a.kind for a in candidate.assets.assets}, {"S", "F", "C", "P"})

    def test_bootstrap_feedback_covers_all_distinct_errors_past_first_twelve(self):
        scenarios = [{"asset_id": "F", "scenario_id": f"stress-{n}", "status": "failed",
                      "required": True, "error": "nullable row type mismatch"}
                     for n in range(16)]
        scenarios += [{"asset_id": "F", "scenario_id": "traverse", "status": "failed",
                       "required": True, "error": "Invalid traversal"},
                      {"asset_id": "C", "scenario_id": "graph", "status": "failed",
                       "required": True, "error": "date result is not a string"}]
        summary = _trial_failure_feedback({"scenarios": scenarios})
        self.assertIn("18 个必需场景失败、3 类独立错误", summary)
        self.assertIn("16 次", summary)
        self.assertIn("Invalid traversal", summary)
        self.assertIn("date result is not a string", summary)

    def test_bootstrap_feedback_names_traversal_coverage_gap(self):
        scenarios = [
            {"asset_id": "f_relation_expand", "scenario_id": direction,
             "status": "incomplete", "required": True,
             "error": "No contract-valid input triggered the required capability path"}
            for direction in ("high_degree_in", "high_degree_out")]
        summary = _trial_failure_feedback({"scenarios": scenarios})
        self.assertIn("2 个必需场景失败、1 类独立错误", summary)
        self.assertIn("high_degree_in, high_degree_out", summary)
        self.assertIn("真实参数绑定", summary)
        self.assertIn("图行序压力副本", summary)
        self.assertNotIn("trial_inputs 含 rows", summary)
