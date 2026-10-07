"""Recorded stability regressions for candidate admission."""

import asyncio
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.agents.protocol import ProtocolError
from darwinagent.config import RunConfig
from darwinagent.contracts import (
    CaseInput,
    QuestionInput,
)
from darwinagent.experiments.admission import AdmissionError
from darwinagent.experiments.bootstrap import AssetBootstrapper
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.experiments.snapshots import load_frozen_graph
from darwinagent.experiments.statistics import stability_metrics
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.artifacts import atomic_json
from tests.support.graphs import (
    build_snapshot,
    cold_bundle,
    corpus,
)
from tests.support.preflight import preflight_sync


class AdmissionTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_missing_remote_vector_keeps_local_replay_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            seed = cold_bundle(root / "seed", with_c=False)
            local = Asset(
                "f_bad_local",
                "F",
                "def run(params):\n"
                " return [str(row['source_ids']) for row in nodes('原子事实',limit=2)]\n",
                {"type": "object", "properties": {}},
                {"type": "array"},
                ["schema"],
                trial_inputs=({},),
            )
            bundle = KernelAssets(seed.assets.assets + (local,)).export(root / "candidate")
            case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "有哪些事实？"),))
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "run",
                snapshot_root=snapshot.parent,
            )
            with mock.patch.dict(
                "os.environ",
                {"EMBEDDING_BASE_URL": "", "EMBEDDING_API_KEY": "", "EMBEDDING_MODEL": ""},
            ):
                with self.assertRaises(AdmissionError):
                    self._preflight_sync(
                        runner,
                        bundle,
                        SimpleNamespace(retrieval_floor={}),
                        cases=(case,),
                        replay_inputs=(("conv-x", "f_bad_local", {}),),
                    )
            report = json.loads((root / "admission.json").read_text())
            self.assertEqual(report["verdict"], "failed")
            self.assertTrue(
                any(
                    row["scenario_id"] == "remote_vector" and row["status"] == "incomplete"
                    for row in report["scenarios"]
                )
            )
            self.assertTrue(
                any(
                    row["scenario_id"] == "replay"
                    and row["asset_id"] == "f_bad_local"
                    and row["status"] == "failed"
                    and "Container-to-string" in row["error"]
                    for row in report["scenarios"]
                )
            )
            self.assertGreater(report["counts"]["conv-x"]["f_bad_local"]["executed"], 0)

    def test_bootstrap_immediate_feedback_uses_frozen_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            second, _ = build_snapshot(root / "second", conv="conv-y")
            shutil.copytree(second, snapshot.parent / "conv-y")
            seed = cold_bundle(root / "seed", with_c=False)
            base = next(a for a in seed.assets.assets if a.kind == "F")
            good = replace(
                base,
                id="f_nodes",
                content="def run(params):\n return nodes('原子事实',limit=2)\n",
                input_contract={"type": "object", "properties": {}},
                output_contract={"type": "array"},
                trial_inputs=({},),
            )
            bad = replace(
                good,
                content="def run(params):\n"
                " return [str(row['source_ids']) for row in nodes('原子事实',limit=2)]\n",
            )
            common = tuple(a for a in seed.assets.assets if a.kind != "F")
            cases = (
                CaseInput("conv-x", corpus(), (QuestionInput("q1", "检索结果是什么？"),)),
                CaseInput("conv-y", corpus(), (QuestionInput("q2", "有哪些相关内容？"),)),
            )
            spec = SimpleNamespace(
                seed_s="",
                requirements=(),
                retrieval_floor={},
                declaration=lambda: {"name": "trial"},
            )
            config = RunConfig(function_timeout_s=15, protocol_attempts=1)

            def client_for(function):
                return RecordedClient(
                    {"bootstrap": [{"assets": [a.to_dict() for a in common + (function,)]}]}
                )

            target = root / "B0" / "assets"
            bundle = asyncio.run(
                AssetBootstrapper().initialize(
                    cases, spec, client_for(good), config, target, snapshot_root=snapshot.parent
                )
            )
            self.assertEqual(bundle.get("f_nodes").fingerprint, good.fingerprint)
            rejected = root / "B0-bad" / "assets"
            with self.assertRaises(ProtocolError) as rejected_error:
                asyncio.run(
                    AssetBootstrapper().initialize(
                        cases,
                        spec,
                        client_for(bad),
                        config,
                        rejected,
                        snapshot_root=snapshot.parent,
                    )
                )
            self.assertIn("Container-to-string", str(rejected_error.exception))
            self.assertFalse((rejected / "manifest.json").exists())

    def test_missing_frozen_graph_writes_incomplete_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = cold_bundle(root / "candidate", with_c=False)
            case = CaseInput("missing", corpus(), (QuestionInput("q1", "事实"),))
            runner = ExperimentRunner(
                None, None, None, RunConfig(), None, root / "run", snapshot_root=root / "snapshots"
            )
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner, candidate, SimpleNamespace(retrieval_floor={}), cases=(case,)
                )
            report = json.loads((root / "candidate" / "admission.json").read_text())
            self.assertEqual(report["verdict"], "failed")
            self.assertEqual(report["scenarios"][0]["scenario_id"], "training_graph")
            self.assertEqual(report["scenarios"][0]["status"], "incomplete")

    def test_answer_check_sees_actual_function_output_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            seed = cold_bundle(root / "seed", with_c=False)
            source = Asset(
                "f_source",
                "F",
                "def run(params):\n"
                " return [{'node_id':r['node_id'],'source_ids':r['source_ids'],"
                "'marker':'from_fn'} for r in nodes('原子事实',limit=2)]\n",
                {"type": "object", "properties": {}},
                {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                ["schema"],
                trial_inputs=({},),
            )
            check = Asset(
                "c_shape",
                "C",
                "def check(candidate):\n"
                " if not candidate.get('answer'):\n"
                "  return {'ok':False,'issues':['empty']}\n"
                " if candidate.get('evidence') and "
                "candidate['evidence'][0].get('marker')=='from_fn':\n"
                "  return {'ok':False,'issues':['bad shape']}\n"
                " return {'ok':True,'issues':[]}\n",
                stage="answer",
                schema_dependencies=["schema"],
            )
            candidate = KernelAssets(
                tuple(a for a in seed.assets.assets if a.kind != "F") + (source, check)
            ).export(root / "candidate")
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "run",
                bootstrap_trial_graph=graph,
            )
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner,
                    candidate,
                    SimpleNamespace(retrieval_floor={}),
                    QuestionInput("q1", "事实"),
                )
            report = json.loads((root / "admission.json").read_text())
            self.assertTrue(
                any(
                    row["scenario_id"] == "function_check"
                    and row["asset_id"] == "c_shape"
                    and row["status"] == "failed"
                    and row["issues"] == ["bad shape"]
                    for row in report["scenarios"]
                )
            )

    def test_function_chain_failure_rejects_individually_passing_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            seed = cold_bundle(root / "seed", with_c=False)
            source = Asset(
                "f_source",
                "F",
                "def run(params):\n"
                " return [{'node_id':r['node_id'],'source_ids':r['source_ids'],"
                "'marker':'from_fn'} for r in nodes('原子事实',limit=2)]\n",
                {"type": "object", "properties": {}},
                {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                ["schema"],
                trial_inputs=({},),
            )
            target = Asset(
                "f_target",
                "F",
                "def run(params):\n"
                " for row in params['rows']:\n"
                "  if row.get('marker')=='from_fn':\n"
                "   return {'rows':[str(row['source_ids'])]}\n"
                " return {'rows':[]}\n",
                {"type": "object", "properties": {"rows": {"type": "array"}}, "required": ["rows"]},
                {"type": "object", "properties": {"rows": {"type": "array"}}, "required": ["rows"]},
                ["schema"],
                trial_inputs=({"rows": []},),
            )
            candidate = KernelAssets(
                tuple(a for a in seed.assets.assets if a.kind != "F") + (source, target)
            ).export(root / "candidate")
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "run",
                bootstrap_trial_graph=graph,
            )
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner,
                    candidate,
                    SimpleNamespace(retrieval_floor={}),
                    QuestionInput("q1", "事实"),
                )
            report = json.loads((root / "admission.json").read_text())
            self.assertTrue(
                any(
                    row["scenario_id"] == "function_chain"
                    and row["asset_id"] == "f_target"
                    and row["source_asset_id"] == "f_source"
                    and row["status"] == "failed"
                    and "Container-to-string" in row["error"]
                    for row in report["scenarios"]
                )
            )
            self.assertTrue(
                any(
                    row["asset_id"] == "f_target"
                    and row["scenario_id"] == "base"
                    and row["status"] == "passed"
                    for row in report["scenarios"]
                )
            )

    def test_stability_metrics_do_not_treat_zero_admissions_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNone(stability_metrics(root)["admission_pass_rate"])
            atomic_json(
                root / "B0" / "admission.json",
                {"verdict": "passed", "elapsed_s": 1.5, "smoke": {"status": "passed"}},
            )
            atomic_json(
                root / "R1" / ".candidate-attempt-0" / "admission.json",
                {"verdict": "failed", "elapsed_s": 0.5},
            )
            atomic_json(
                root / "R1" / "stage.json", {"scores": {"generation_faults": 1}, "elapsed_s": 12}
            )
            atomic_json(
                root / "R1" / "fault-retry" / "trial.json",
                {
                    "questions": {
                        "q1": {
                            "initial_error_type": "TransportExhausted",
                            "final_error_type": "TransportExhausted",
                        }
                    }
                },
            )
            metrics = stability_metrics(root)
            self.assertEqual(metrics["admission_pass_rate"], 0.5)
            self.assertEqual(metrics["smoke_passed"], 1)
            self.assertEqual(metrics["formal_execution_faults"], 1)
            self.assertEqual(metrics["retry_same_class_failures"], 1)
