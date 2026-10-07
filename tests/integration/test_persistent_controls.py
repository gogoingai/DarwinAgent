"""Human events survive restart and stay attached to the selected branch."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.agents.protocol import ProtocolError
from darwinagent.config import Config, RunConfig
from darwinagent.experiments.control import intervene
from darwinagent.experiments.policy import AdoptionPolicy
from darwinagent.experiments.proposal_session import ProposalSession, decode_action
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.experiments.wiki_service import WikiService
from darwinagent.runtime.execution import ExecutionSelection
from darwinagent.runtime.workspace import Workspace
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK, case, client, spec
from tests.support.recorded_wiki import WikiRecordedExperiment


class PersistentControlsTests(unittest.TestCase):
    def runner(self, root, evaluator_factory=None):
        c = case()
        runner = ExperimentRunner(
            type("Adapter", (), {"generation_input": lambda _, _id: c})(),
            evaluator_factory or (lambda _c, _p: None),
            Config(model_strong="configured-model"),
            RunConfig(),
            AdoptionPolicy("score", ()),
            root,
            client_factory=lambda _: LedgerRecordedClient({}),
        )
        return runner, c

    def test_configuration_and_policy_survive_restart_without_secret_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intervene(
                root,
                {
                    "kind": "configuration",
                    "run_config": {"concurrency": 1},
                    "connection_config": {
                        "model_strong": "human-specified",
                        "api_key": "must-not-be-stored",
                    },
                },
            )
            intervene(
                root,
                {
                    "kind": "policy",
                    "policy": {"primary": "accuracy", "non_decreasing": ["coverage"]},
                },
            )
            runner, _ = self.runner(root)
            self.assertEqual(runner.config.concurrency, 1)
            self.assertEqual(runner.connection_config.model_strong, "human-specified")
            self.assertEqual(runner.connection_config.api_key, "")
            self.assertEqual(runner.policy.primary, "accuracy")
            self.assertEqual(runner.policy.non_decreasing, ("coverage",))
            self.assertNotIn(
                b"must-not-be-stored",
                b"".join(p.read_bytes() for p in (root / "workspace").rglob("*") if p.is_file()),
            )

    def test_other_branch_controls_do_not_leak_main_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intervene(root, {"kind": "configuration", "run_config": {"concurrency": 1}})
            intervene(
                root, {"kind": "configuration", "run_config": {"concurrency": 3}}, branch="fork"
            )
            runner, c = self.runner(root)
            asyncio.run(
                runner.run(
                    c.id,
                    spec(root / "assets"),
                    execution=ExecutionSelection(branch="fork", stages=("report",)),
                )
            )
            self.assertEqual(runner.config.concurrency, 3)
            runner._refresh_controls("main")
            self.assertEqual(runner.config.concurrency, 1)

    def test_pause_stop_and_resume_are_real_execution_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intervene(root, {"kind": "pause"})
            runner, c = self.runner(root)
            task = spec(root / "assets")
            self.assertEqual(asyncio.run(runner.run(c.id, task))["status"], "paused")
            self.assertFalse((root / "B0").exists())
            intervene(root, {"kind": "stop"})
            restarted, _ = self.runner(root)
            self.assertEqual(asyncio.run(restarted.run(c.id, task))["status"], "stopped")
            intervene(root, {"kind": "resume"})
            self.assertEqual(
                asyncio.run(
                    restarted.run(c.id, task, execution=ExecutionSelection(stages=("report",)))
                )["status"],
                "complete",
            )
            self.assertTrue((root / "control-boundaries/main.json").exists())

    def test_code_event_stops_existing_executor_but_new_executor_can_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, c = self.runner(root)
            task = spec(root / "assets")
            runner.intervene({"kind": "code", "reason": "changed local implementation"})
            result = asyncio.run(runner.run(c.id, task))
            self.assertTrue(result["requires_restart"])
            fresh, _ = self.runner(root)
            self.assertEqual(
                asyncio.run(
                    fresh.run(c.id, task, execution=ExecutionSelection(stages=("report",)))
                )["status"],
                "complete",
            )

    def test_wiki_correction_is_applied_once_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = WikiService(root)
            original = service.register({"fact": "old hypothesis"}, scope={"case_ids": ["case-a"]})
            intervene(
                root,
                {
                    "kind": "wiki_correction",
                    "target_ref": original,
                    "text": "counterexample contradicts attribution",
                },
            )
            runner, _ = self.runner(root)
            runner._refresh_controls()
            applied = Workspace(root / "workspace").events(kind="wiki_control_applied")
            self.assertEqual(len(applied), 1)
            self.assertEqual(service._read(original)["data"]["fact"], "old hypothesis")

    def test_evaluation_settings_change_the_next_evaluator_after_restart(self):
        class Evaluator:
            def __init__(self):
                self.standard = "old"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intervene(root, {"kind": "evaluation", "evaluation_config": {"standard": "new"}})
            runner, _ = self.runner(root, lambda _client, _path: Evaluator())
            self.assertEqual(runner.evaluator_factory(None, root).standard, "new")

    def test_explicit_new_attempt_resumes_proposal_dialogue_on_replacement_request(self):
        class Broken:
            async def chat(self, **_kwargs):
                raise OSError("response unknown")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            session = ProposalSession(
                root / "proposal.json",
                payload={"base_version": "base"},
                protocol="protocol",
                workspace=workspace,
            )
            config = SimpleNamespace(proposal_role="proposal", temperature=0, protocol_attempts=1)
            with self.assertRaises(ProtocolError):
                asyncio.run(session.run(Broken(), config, decode_action))
            original = session.state["request_id"]
            workspace.recover_requests()
            from darwinagent.cli import main

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "workspace",
                        "--root",
                        str(root),
                        "request",
                        "resolve",
                        original,
                        "--action",
                        "new-attempt",
                    ]
                )
            self.assertEqual(status, 0)
            replacement = json.loads(output.getvalue())["request_id"]
            transport = LedgerRecordedClient(
                {"proposal": [{"action": "no_change", "reason": "recovered dialogue"}]}
            )
            result = asyncio.run(session.run(transport, config, decode_action))
            self.assertEqual(result["reason"], "recovered dialogue")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(session.state["request_id"], replacement)
            self.assertEqual(workspace.request(replacement)["status"], "responded")
            self.assertEqual(workspace.request(original)["status"], "abandoned")
            self.assertEqual(workspace.replacement_for(original)["id"], replacement)

    def test_nonmain_round_updates_only_its_own_branch(self):
        class ForkRunner(WikiRecordedExperiment):
            def _client(self, stage):
                if stage == "R1" and not self.stage_clients[stage]:
                    self.stage_clients[stage] += 1
                    from darwinagent.kernel.registration import load_assets
                    from darwinagent.kernel.revision import training_id

                    asset = next(a for a in load_assets(TASK).assets if a.role == "answer")
                    updated = asset.to_dict()
                    updated["content"] += "\nCheck the current source carefully."
                    return LedgerRecordedClient(
                        {
                            "proposal": [
                                {
                                    "patches": [
                                        {
                                            "asset": updated,
                                            "base_fingerprint": "current:" + asset.id,
                                            "reason": "source check",
                                            "training_evidence": [training_id(self.case.id, "q1")],
                                        }
                                    ]
                                }
                            ]
                        }
                    )
                if stage == "R1":
                    return LedgerRecordedClient(client(self.case).replies)
                return super()._client(stage)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            main = workspace.create_branch(
                "main", adopted="protected-adopted", working="protected-working"
            )
            workspace.create_branch("fork")
            runner = ForkRunner(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(
                    runner.run(
                        runner.case.id,
                        spec(root / "assets"),
                        rounds=1,
                        scope=("S", "F", "C", "P"),
                        execution=ExecutionSelection(branch="fork"),
                    )
                )
            self.assertEqual(workspace.branch("main"), main)
            self.assertEqual(
                workspace.branch("fork")["working"], summary["rounds"][0]["candidate_version"]
            )

    def test_client_boundary_observes_pause_and_live_configuration_before_next_request(self):
        from darwinagent.experiments.control import ControlSignal

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, _ = self.runner(root)
            transport = runner._client("B0")
            intervene(root, {"kind": "pause"})
            with self.assertRaises(ControlSignal) as paused:
                transport._control_boundary("model:answer")
            self.assertEqual(paused.exception.state["status"], "paused")
            intervene(root, {"kind": "resume"})
            intervene(root, {"kind": "configuration", "connection_config": {"model_strong": "new"}})
            with self.assertRaises(ControlSignal) as changed:
                transport._control_boundary("model:answer")
            self.assertTrue(changed.exception.state["requires_restart"])
            fresh, _ = self.runner(root)
            self.assertEqual(
                fresh._client("B0")._control_boundary("model:answer")["status"], "running"
            )

    def test_invalid_wiki_draft_is_reported_without_poisoning_other_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intervene(root, {"kind": "wiki_correction", "text": "", "target_ref": "missing"})
            runner, _ = self.runner(root)
            runner._refresh_controls()
            rejected = Workspace(root / "workspace").events(kind="wiki_control_rejected")
            self.assertEqual(len(rejected), 1)
            self.assertIn("nonempty text", rejected[0]["payload"]["error"])
