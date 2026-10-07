"""Publication races, maintenance independence and active-budget recovery."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.experiments.rounds import _select_candidate
from darwinagent.kernel import TaskSpec
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.workspace import Workspace
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK
from tests.support.recorded_fastloop import FastLoopExperiment, _run
from tests.support.recorded_wiki import WikiRecordedExperiment


class RoundInterventionTests(unittest.TestCase):
    def test_old_automatic_result_cannot_overwrite_human_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            original = workspace.create_branch("main", adopted="b0", working="b0")
            workspace.select_branch(
                "main",
                expected_revision=original["revision"],
                working="human-candidate",
                source="human",
            )
            revisions = mock.Mock()
            candidate = SimpleNamespace(version="auto-candidate", root=root / "candidate")
            decision = {"accepted": True}
            self.assertFalse(
                _select_candidate(
                    workspace, "main", original["revision"], candidate, decision, revisions, root
                )
            )
            revisions.publish.assert_not_called()
            self.assertEqual(workspace.branch("main")["working"], "human-candidate")
            self.assertEqual(workspace.branch("main")["adopted"], "b0")
            self.assertEqual(decision["selection_status"], "detached_after_human_change")
            self.assertEqual(
                json.loads((root / "publication-outbox/auto-candidate.json").read_text())["state"],
                "detached",
            )

    def test_quality_rejection_keeps_working_candidate_without_adopting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            original = workspace.create_branch("main", adopted="b0", working="b0")
            revisions = mock.Mock()
            candidate = SimpleNamespace(version="candidate", root=root / "candidate")
            self.assertTrue(
                _select_candidate(
                    workspace,
                    "main",
                    original["revision"],
                    candidate,
                    {"accepted": False},
                    revisions,
                    root,
                )
            )
            self.assertEqual(workspace.branch("main")["working"], "candidate")
            self.assertEqual(workspace.branch("main")["adopted"], "b0")
            revisions.publish.assert_not_called()

    def test_offline_expired_wall_clock_does_not_consume_active_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FastLoopExperiment(root, round_deadline_s=30)
            _run(runner, rounds=0)
            atomic_json(
                root / "R1/round-budget.json",
                {"limit_s": 30, "spent_s": 0, "started_at": 0, "deadline_at": 1},
            )
            resumed = FastLoopExperiment(root, round_deadline_s=30)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(
                    resumed.run(
                        resumed.case.id,
                        TaskSpec.load(TASK / "task.yaml"),
                        rounds=1,
                        resume=True,
                        scope=("S", "F", "C", "P"),
                    )
                )
            self.assertEqual(summary["completed_rounds"], 1)
            self.assertNotEqual(summary["rounds"][0].get("status"), "round_timeout")
            saved = json.loads((root / "R1/round-budget.json").read_text())
            self.assertGreater(saved["spent_s"], 0)
            self.assertLess(saved["spent_s"], 30)

    def test_changed_criterion_blocks_comparison_but_preserves_working_candidate(self):
        class ChangedCriterion(WikiRecordedExperiment):
            async def _stage(self, name, *args, **kwargs):
                result = await super()._stage(name, *args, **kwargs)
                path = self.root / name / "stage.json"
                row = json.loads(path.read_text())
                row["criterion_id"] = "old-standard" if name == "B0" else "new-standard"
                if name == "B0":
                    row["comparison_reliable"] = False
                atomic_json(path, row)
                return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ChangedCriterion(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(
                    runner.run(
                        runner.case.id,
                        TaskSpec.load(TASK / "task.yaml"),
                        rounds=1,
                        scope=("S", "F", "C", "P"),
                    )
                )
            decision = summary["rounds"][0]
            self.assertFalse(decision["accepted"])
            self.assertFalse(decision["comparison_reliable"])
            self.assertTrue(any("criterion_mismatch" in reason for reason in decision["reasons"]))
            self.assertEqual(summary["working_version"], decision["candidate_version"])
            self.assertEqual(summary["adopted_version"], decision["base_version"])
            self.assertTrue((root / "R1/candidate/bundle/manifest.json").exists())

    def test_no_change_is_normal_and_avoids_answer_calls(self):
        class NoChange(FastLoopExperiment):
            def _client(self, stage):
                if stage == "R1":
                    return LedgerRecordedClient(
                        {
                            "proposal": [
                                {
                                    "action": "no_change",
                                    "reason": "no supported change",
                                    "unresolved": ["q3"],
                                }
                            ]
                        }
                    )
                return super()._client(stage)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = _run(NoChange(root), rounds=1)
            self.assertEqual(summary["rounds"][0]["status"], "no_change")
            self.assertEqual(summary["status"], "complete")
            self.assertFalse((root / "R1/generation").exists())
            self.assertTrue((root / "R1/optimization/attempt-0/proposal-call.json").exists())
