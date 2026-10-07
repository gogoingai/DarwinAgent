"""Public phase selection never expands into generation or grading."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.config import Config, RunConfig
from darwinagent.engine import Pipeline
from darwinagent.experiments.policy import AdoptionPolicy
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.execution import ExecutionSelection
from tests.support.clients import LedgerRecordedClient
from tests.support.device import case, client, spec
from tests.support.recorded_wiki import WikiRecordedExperiment


class SelectedOperationsTests(unittest.TestCase):
    def runner(self, root, replies=None, snapshot_root=None):
        c = case("conv-26") if snapshot_root is not None else case()
        transports = []

        def transport(_stage):
            value = LedgerRecordedClient(replies or {})
            transports.append(value)
            return value

        evaluator = mock.Mock(side_effect=AssertionError("Unselected scoring"))
        runner = ExperimentRunner(
            type("Adapter", (), {"generation_input": lambda _, _id: c})(),
            evaluator,
            Config(),
            RunConfig(protocol_attempts=1),
            AdoptionPolicy("score", ()),
            root,
            client_factory=transport,
            snapshot_root=snapshot_root,
        )
        return runner, c, transports

    def test_report_without_answers_has_zero_clients_and_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(root)
            result = asyncio.run(
                runner.run(
                    c.id, spec(root / "assets"), execution=ExecutionSelection(stages=("report",))
                )
            )
            self.assertEqual(result["operations"]["report"]["status"], "complete")
            self.assertTrue(Path(result["operations"]["report"]["path"]).exists())
            self.assertEqual(clients, [])
            runner.evaluator_factory.assert_not_called()
            self.assertFalse((root / "B0").exists())

    def test_frozen_vector_registration_never_initializes_embedder(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/locomo_snapshot"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(root, snapshot_root=fixture)
            with mock.patch(
                "darwinagent.vector.load_embedder", side_effect=AssertionError("No model probe")
            ):
                result = asyncio.run(
                    runner.run(
                        c.id,
                        spec(root / "assets"),
                        execution=ExecutionSelection(stages=("vector",)),
                    )
                )
            self.assertEqual(result["operations"]["vector"]["status"], "complete")
            self.assertEqual(result["operations"]["vector"]["records"][0]["rows"], 4)
            self.assertEqual(clients, [])

    def test_missing_vector_and_candidate_graph_are_gaps_without_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(root)
            result = asyncio.run(
                runner.run(
                    c.id,
                    spec(root / "assets"),
                    execution=ExecutionSelection(stages=("vector", "candidate_check")),
                )
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["operations"]["vector"]["status"], "missing_inputs")
            self.assertEqual(result["operations"]["candidate_check"]["status"], "missing_inputs")
            self.assertEqual(clients, [])
            self.assertFalse((root / "B0").exists())

    def test_explicit_proposal_only_uses_proposer_and_saves_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(
                root,
                {
                    "proposal": [
                        {"action": "no_change", "reason": "training evidence needs verification"}
                    ]
                },
            )
            result = asyncio.run(
                runner.run(
                    c.id, spec(root / "assets"), execution=ExecutionSelection(stages=("proposal",))
                )
            )
            self.assertEqual(result["operations"]["proposal"]["patch_count"], 0)
            self.assertEqual([row["role"] for t in clients for row in t.calls], ["proposal"])
            runner.evaluator_factory.assert_not_called()
            self.assertFalse((root / "B0").exists())
            saved = json.loads(Path(result["operations"]["proposal"]["path"]).read_text())
            self.assertEqual(saved["action"]["action"], "no_change")

    def test_candidate_check_executes_existing_checker_without_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(root)
            task = spec(root / "assets")
            asyncio.run(
                Pipeline(client(c), root / "B0/generation").run(
                    c, task, RunConfig(protocol_attempts=1)
                )
            )
            result = asyncio.run(
                runner.run(c.id, task, execution=ExecutionSelection(stages=("candidate_check",)))
            )
            self.assertEqual(result["operations"]["candidate_check"]["status"], "complete")
            self.assertEqual(result["operations"]["candidate_check"]["report"]["verdict"], "passed")
            self.assertEqual(clients, [])
            runner.evaluator_factory.assert_not_called()

    def test_wiki_replays_saved_maintenance_without_neighbor_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(root)
            WikiMaintainer(root, "existing", runner._client, runner.config)
            atomic_json(
                root / "R1/wiki-outbox/pending.json",
                {
                    "state": "pending",
                    "args": ["R1", "decision", {"status": "recorded"}],
                    "kwargs": {"infer": False, "training_ids": []},
                },
            )
            result = asyncio.run(
                runner.run(
                    c.id, spec(root / "assets"), execution=ExecutionSelection(stages=("wiki",))
                )
            )
            self.assertTrue(result["operations"]["wiki"]["replayed"])
            entries = json.loads((root / "optimization/wiki.json").read_text())["entries"]
            self.assertTrue(any(entry["stage"] == "R1" for entry in entries))
            self.assertEqual(clients, [])
            runner.evaluator_factory.assert_not_called()
            self.assertFalse((root / "B0").exists())

    def test_unmatched_question_scope_reports_gap_without_proposal_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(root)
            result = asyncio.run(
                runner.run(
                    c.id,
                    spec(root / "assets"),
                    execution=ExecutionSelection(
                        stages=("proposal", "report"), question_ids=("missing-q",)
                    ),
                )
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["operations"]["proposal"]["status"], "missing_inputs")
            self.assertTrue(result["missing_scope"])
            self.assertEqual(result["operations"]["report"]["status"], "complete")
            self.assertEqual(clients, [])

    def test_explicit_proposal_rerun_creates_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(
                root, {"proposal": [{"action": "no_change", "reason": "no defensible change"}]}
            )
            task = spec(root / "assets")
            selection = ExecutionSelection(mode="rerun", stages=("proposal",))
            first = asyncio.run(runner.run(c.id, task, execution=selection))
            second = asyncio.run(runner.run(c.id, task, execution=selection))
            self.assertNotEqual(
                first["operations"]["proposal"]["path"], second["operations"]["proposal"]["path"]
            )
            self.assertEqual(sum(len(t.calls) for t in clients), 2)
            self.assertFalse(any(call["use_cache"] for t in clients for call in t.calls))

    def test_manual_asset_registration_becomes_actual_proposal_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c, clients = self.runner(
                root, {"proposal": [{"action": "no_change", "reason": "manual candidate retained"}]}
            )
            task = spec(root / "assets")
            assets = [asset.to_dict() for asset in task.bundle.assets.assets]
            for asset in assets:
                if asset.get("role") == "answer":
                    asset["content"] += "\n人工要求：优先核对日期。"
            intervention = runner.intervene({"kind": "assets", "assets": assets})
            result = asyncio.run(
                runner.run(c.id, task, execution=ExecutionSelection(stages=("proposal",)))
            )
            saved = json.loads(Path(result["operations"]["proposal"]["path"]).read_text())
            self.assertEqual(saved["input"]["base_version"], intervention["branch"]["working"])
            self.assertTrue(
                any("人工要求" in asset["content"] for asset in saved["input"]["assets"])
            )
            self.assertEqual([call["role"] for t in clients for call in t.calls], ["proposal"])

    def test_human_objective_takes_priority_over_existing_wiki_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WikiRecordedExperiment(root)
            runner.intervene(
                {"kind": "objective", "objective": "先排查分页是否推进，不追求分数上涨"}
            )
            asyncio.run(
                runner.run(
                    runner.case.id, spec(root / "assets"), rounds=1, scope=("S", "F", "C", "P")
                )
            )
            goal = json.loads((root / "R1/optimization/goal.json").read_text())
            self.assertEqual(goal["direction"], "先排查分页是否推进，不追求分数上涨")
            self.assertEqual(goal["objective_source"], "human")
