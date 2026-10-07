"""Recorded stability regressions for worker isolation."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    CaseInput,
    QuestionInput,
)
from darwinagent.experiments.admission import AdmissionError
from darwinagent.experiments.admission_worker import run_isolated
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.experiments.snapshots import attach_vector, load_frozen_graph
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.operators.data import DataCapabilities
from darwinagent.runtime.artifacts import atomic_json
from tests.support.graphs import (
    FakeEmbedder,
    build_snapshot,
    cold_bundle,
    corpus,
)
from tests.support.preflight import preflight_sync


class WorkerIsolationTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_timeout_preserves_independent_completed_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "request.json"
            report = root / "report.json"
            payload.write_text(json.dumps({"bundle_version": "candidate", "cases": []}))

            def blocks(*args, **kwargs):
                atomic_json(
                    report,
                    {
                        "candidate_version": "candidate",
                        "verdict": "incomplete",
                        "scenarios": [
                            {"asset_id": "f_one", "status": "failed", "error": "known failure"},
                            {"asset_id": "f_two", "status": "passed"},
                        ],
                    },
                )
                raise subprocess.TimeoutExpired("worker", 1)

            with mock.patch(
                "darwinagent.experiments.admission_worker.subprocess.run", side_effect=blocks
            ):
                self.assertFalse(run_isolated(payload, report, 1))
            saved = json.loads(report.read_text())
            self.assertEqual(saved["verdict"], "timeout")
            self.assertEqual(
                [r["asset_id"] for r in saved["scenarios"]], ["f_one", "f_two", "bundle"]
            )

    def test_zero_exit_without_report_does_not_admit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "request.json"
            report = root / "report.json"
            payload.write_text(json.dumps({"bundle_version": "candidate", "cases": []}))
            self.assertFalse(
                run_isolated(payload, report, 1, command=[sys.executable, "-c", "pass"])
            )
            saved = json.loads(report.read_text())
            self.assertEqual(saved["verdict"], "failed")
            self.assertEqual(saved["scenarios"][0]["scenario_id"], "worker_report")

    def test_real_frozen_worker_accepts_good_and_keeps_bad_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            second_snapshot, _ = build_snapshot(root / "second", conv="conv-y")
            shutil.copytree(second_snapshot, snapshot.parent / "conv-y")
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
            common = tuple(a for a in seed.assets.assets if a.kind != "F")
            working = KernelAssets(common + (good,)).export(root / "working")
            broken = replace(
                good,
                id="f_bad",
                content="def run(params):\n"
                " rows=nodes('原子事实',limit=2)\n"
                " return [str(row['source_ids']) for row in rows]\n",
            )
            failing = KernelAssets(common + (broken,)).export(root / "failing")
            case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "事实"),))
            second = CaseInput("conv-y", corpus(), (QuestionInput("q2", "事实"),))
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "run",
                snapshot_root=snapshot.parent,
            )
            healthy = self._preflight_sync(
                runner, working, SimpleNamespace(retrieval_floor={}), cases=(case, second)
            )
            self.assertEqual(healthy["verdict"], "passed")
            self.assertGreater(healthy["counts"]["conv-x"]["f_nodes"]["executed"], 0)
            self.assertGreater(healthy["counts"]["conv-y"]["f_nodes"]["executed"], 0)
            self.assertTrue(
                any(
                    row["scenario_id"] == "invalid_params_rejected" and row["status"] == "passed"
                    for row in healthy["scenarios"]
                )
            )
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner, failing, SimpleNamespace(retrieval_floor={}), cases=(case, second)
                )
            report = json.loads((root / "admission.json").read_text())
            self.assertEqual(report["candidate_version"], failing.version)
            self.assertTrue(
                any(
                    row["asset_id"] == "f_bad"
                    and row["status"] == "failed"
                    and "Container-to-string" in (row.get("error") or "")
                    for row in report["scenarios"]
                )
            )
            self.assertEqual(
                report["snapshot_digests"]["conv-x"], healthy["snapshot_digests"]["conv-x"]
            )
            self.assertEqual(set(report["snapshot_digests"]), {"conv-x", "conv-y"})

    def test_failed_worker_preserves_detailed_admission_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "request.json"
            report = root / "report.json"
            payload.write_text(json.dumps({"bundle_version": "candidate", "cases": []}))

            def refused(*args, **kwargs):
                atomic_json(
                    report,
                    {
                        "candidate_version": "candidate",
                        "verdict": "failed",
                        "scenarios": [
                            {"asset_id": "f_one", "status": "failed"},
                            {"asset_id": "f_two", "status": "failed"},
                        ],
                    },
                )
                return SimpleNamespace(returncode=1, stderr="AdmissionError")

            with mock.patch(
                "darwinagent.experiments.admission_worker.subprocess.run", side_effect=refused
            ):
                self.assertFalse(run_isolated(payload, report, 1))
            rows = json.loads(report.read_text())["scenarios"]
            self.assertEqual([row["asset_id"] for row in rows], ["f_one", "f_two"])

    def test_worker_failure_replaces_stale_passed_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "request.json"
            report = root / "report.json"
            payload.write_text(json.dumps({"bundle_version": "changed", "cases": []}))
            report.write_text(json.dumps({"verdict": "passed", "candidate_version": "old"}))
            self.assertFalse(
                run_isolated(
                    payload,
                    report,
                    1,
                    command=[sys.executable, "-c", 'raise RuntimeError("worker failed")'],
                )
            )
            row = json.loads(report.read_text())
            self.assertEqual(row["verdict"], "failed")
            self.assertEqual(row["candidate_version"], "changed")
            self.assertEqual(row["scenarios"][0]["scenario_id"], "worker_exit")

    def test_blocked_child_is_killed_and_timeout_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "request.json"
            report = root / "report.json"
            payload.write_text(json.dumps({"bundle_version": "frozen", "cases": [{"id": "train"}]}))
            self.assertFalse(
                run_isolated(
                    payload,
                    report,
                    0.2,
                    command=[sys.executable, "-c", "import time; time.sleep(10)"],
                )
            )
            row = json.loads(report.read_text())
            self.assertEqual(row["verdict"], "timeout")
            self.assertEqual(row["candidate_version"], "frozen")
            self.assertEqual(row["cases"], ["train"])

    def test_bad_function_fails_pressure_and_independent_checks_continue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())
            baseline = cold_bundle(root / "seed", with_c=False)
            rows_contract = {
                "type": "object",
                "properties": {"rows": {"type": "array"}},
                "required": ["rows"],
            }
            bad = Asset(
                "f_bad",
                "F",
                "def run(params):\n out=[]\n for row in params['rows']:\n  out.append(str(row.get('source_ids')))\n return {'rows':out}\n",
                rows_contract,
                {"type": "object", "properties": {"rows": {"type": "array"}}},
                ["schema"],
                trial_inputs=({"rows": []},),
            )
            good = replace(
                bad,
                id="f_good",
                content=(
                    "def run(params):\n"
                    " return {'rows': project(params['rows'], ['node_id','source_ids'])}\n"
                ),
            )
            assets = [a for a in baseline.assets.assets if a.kind != "F"] + [bad, good]
            candidate = KernelAssets(tuple(assets)).export(root / "candidate")
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "new-run",
                bootstrap_trial_graph=graph,
            )
            question = QuestionInput("q1", "哪些事实")
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner,
                    candidate,
                    SimpleNamespace(retrieval_floor={}),
                    question,
                    replay_inputs=(
                        ("f_bad", {"rows": list(DataCapabilities(graph).rows.values())[:1]}),
                    ),
                )
            report = json.loads((root / "admission.json").read_text())
            bad_rows = [x for x in report["scenarios"] if x["asset_id"] == "f_bad"]
            self.assertTrue(
                any(x["scenario_id"] == "stress" and x["status"] == "failed" for x in bad_rows)
            )
            self.assertTrue(
                any(x["scenario_id"] == "replay" and x["status"] == "failed" for x in bad_rows)
            )
            self.assertTrue(
                any(
                    x["asset_id"] == "f_good" and x["status"] == "passed"
                    for x in report["scenarios"]
                )
            )
            self.assertGreater(report["counts"]["trial"]["f_bad"]["stress_legal"], 0)
            self.assertGreater(report["counts"]["trial"]["f_bad"]["executed"], 1)
