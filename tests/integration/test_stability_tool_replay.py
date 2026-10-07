"""Recorded stability regressions for tool replay."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    QuestionInput,
)
from darwinagent.experiments.admission import AdmissionError
from darwinagent.experiments.recovery import (
    _prior_failed_tool_params,
)
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.experiments.snapshots import attach_vector, load_frozen_graph
from darwinagent.runtime.artifacts import atomic_json
from tests.support.graphs import (
    FakeEmbedder,
    build_snapshot,
    cold_bundle,
    corpus,
)
from tests.support.preflight import preflight_sync


class ToolReplayTests(unittest.TestCase):
    _preflight_sync = staticmethod(preflight_sync)

    def test_saved_tool_failure_becomes_next_candidate_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())
            candidate = cold_bundle(root / "candidate", with_c=False)
            aid = next(a.id for a in candidate.assets.assets if a.kind == "F")
            run = root / "run"
            atomic_json(
                run / "R1" / "generation" / "trial" / "answers" / "failed.json",
                {
                    "result": {
                        "status": "execution_error",
                        "error": "ValueError: old failure",
                        "trace": [
                            {
                                "stage": "tool_error",
                                "asset_id": aid,
                                "parameters": {"query": "打印机"},
                            }
                        ],
                    }
                },
            )
            self.assertEqual(_prior_failed_tool_params(run), [("trial", aid, {"query": "打印机"})])
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                run,
                bootstrap_trial_graph=graph,
            )
            report = self._preflight_sync(
                runner, candidate, SimpleNamespace(retrieval_floor={}), QuestionInput("q1", "事实")
            )
            self.assertTrue(
                any(
                    row["scenario_id"] == "replay"
                    and row["asset_id"] == aid
                    and row["status"] == "passed"
                    for row in report["scenarios"]
                )
            )

    def test_unmigrated_historical_params_block_admission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())
            candidate = cold_bundle(root / "candidate", with_c=False)
            runner = ExperimentRunner(
                None,
                None,
                None,
                RunConfig(function_timeout_s=15),
                None,
                root / "new-run",
                bootstrap_trial_graph=graph,
            )
            aid = next(a.id for a in candidate.assets.assets if a.kind == "F")
            healthy = self._preflight_sync(
                runner, candidate, SimpleNamespace(retrieval_floor={}), QuestionInput("q1", "事实")
            )
            self.assertEqual(healthy["verdict"], "passed")
            self.assertTrue(healthy["framework_digest"])
            self.assertTrue(healthy["snapshot_digests"]["trial"])
            self.assertGreater(
                healthy["counts"]["trial"][aid]["by_scenario"]["stress"]["executed"], 0
            )
            with self.assertRaises(AdmissionError):
                self._preflight_sync(
                    runner,
                    candidate,
                    SimpleNamespace(retrieval_floor={}),
                    QuestionInput("q1", "事实"),
                    replay_inputs=((aid, {"unexpected_parameter": 1}),),
                )
            report = json.loads((root / "candidate" / "admission.json").read_text())
            self.assertTrue(
                any(
                    row["scenario_id"] == "replay"
                    and row["status"] == "incomplete"
                    and row["required"]
                    for row in report["scenarios"]
                )
            )
