"""Generic facades, holdout isolation, source binding and durable paired selection."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.agent import EpisodeResult, ExecutionBackend, ExecutionProfile, file_tools
from darwinagent.config import Config, RunConfig
from darwinagent.experiments import (
    AgentExperimentSpec,
    CampaignController,
    EpisodeEvaluation,
    ExperimentRunner,
    GraphQACompatibilityBackend,
)
from darwinagent.experiments.episode_contracts import episode_metrics, select_episode_candidate
from darwinagent.experiments.episode_experiment import read_record
from darwinagent.experiments.episode_proposal import decode_candidate
from tests.support.agent_runtime import (
    LostReceiptClient,
    ModelTwiceAgent,
    ReturnsUnknownAgent,
    ScriptedAgent,
    bundle,
)
from tests.support.episode_experiment import (
    ArtifactEvaluator,
    AsyncIsolatedFileBackend,
    BoundFactories,
    BoundPrimitiveBackend,
    ChangedProfileBackend,
    DerivedReceiptBackend,
    IsolatedFileBackend,
    ProposalClient,
    SharedWorkspaceBackend,
    TaskDataset,
    bound_file_tools,
    candidate_action,
)


class EpisodeExperimentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dataset = TaskDataset()
        self.tools = file_tools(self.root / "host")
        self.profile = ExecutionProfile("files", tuple(t.name for t in self.tools))
        self.kernel = bundle(self.root / "k0", self.profile)
        self.backend = IsolatedFileBackend(
            ScriptedAgent(), self.tools, self.profile, run_root=self.root / "runs"
        )
        self.spec = AgentExperimentSpec(("train",), ("val",), ("admit",), ("test",))
        self.evaluator = ArtifactEvaluator()
        self.client = ProposalClient([candidate_action(self.kernel)])

    def controller(self, *, runner=False, backend=None, spec=None):
        def evaluator_factory(client, path):
            return self.evaluator

        def client_factory(stage):
            return self.client

        arguments = (self.dataset, evaluator_factory, Config(), RunConfig())
        if runner:
            return ExperimentRunner(
                *arguments,
                None,
                self.root / "experiment",
                backend=backend or self.backend,
                validation_plan=spec or self.spec,
                client_factory=client_factory,
            )
        return CampaignController(
            *arguments,
            spec or self.spec,
            self.root / "experiment",
            backend=backend or self.backend,
            client_factory=client_factory,
        )

    async def test_joint_candidate_artifacts_selection_resume_and_holdout_isolation(self):
        controller = self.controller()
        result = await controller.run(self.kernel)
        self.assertEqual(result["status"], "complete")
        self.assertNotEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(result["decisions"][0]["reason"], "quality_improvement")
        self.assertEqual(len(result["accepted_versions"]), 2)
        self.assertEqual(self.client.calls, 1)
        from darwinagent.agent import AgentKernelBundle

        selected = AgentKernelBundle(result["selected_path"])
        self.assertEqual(selected.content_id, result["selected"])
        self.assertTrue(
            all(selected.get(a.id).fingerprint != a.fingerprint for a in self.kernel.assets.assets)
        )
        self.assertEqual(selected.get("final").kind, "C")
        artifacts = list((self.root / "experiment/episodes").glob("*/environment/report.txt"))
        self.assertEqual(len(artifacts), 8)
        self.assertEqual({p.read_text() for p in artifacts}, {"first", "SECOND"})
        self.assertFalse((self.root / "host/report.txt").exists())
        originals = "".join(
            p.read_text() for p in (self.root / "experiment/optimization").rglob("*.json")
        )
        self.assertNotIn("PRIVATE-GOLD-SENTINEL", originals)
        for private_id in ("val", "admit", "test"):
            self.assertNotIn('"id": "' + private_id + '"', originals)
        self.assertNotIn("PRIVATE-GOLD-SENTINEL", json.dumps(self.client.messages))
        resumed = await self.controller().run(self.kernel, resume=True)
        self.assertEqual(resumed, result)
        self.assertEqual(self.client.calls, 1)

    async def test_runner_facade_uses_tasks_directly(self):
        result = await self.controller(runner=True).run(self.spec.train, self.kernel, rounds=1)
        self.assertNotEqual(result["selected"], self.kernel.content_id)

    async def test_no_change_retains_baseline_and_stops_proposing(self):
        self.client = ProposalClient([{"action": "no_change", "reason": "No training evidence"}])
        result = await self.controller(spec=replace(self.spec, rounds=3)).run(self.kernel)
        self.assertEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(result["decisions"][0]["reason"], "no_change")
        self.assertEqual(self.client.calls, 1)

    async def test_schema_source_rejection_retains_candidate_and_reason(self):
        action = candidate_action(self.kernel)
        next(a for a in action["patches"]["assets"] if a["kind"] == "S")["content"] = (
            "nodes: [{id: Evil, source: private_gold, fields: {}, where: {}}]\nrelations: []"
        )
        self.client = ProposalClient([action])
        result = await self.controller().run(self.kernel)
        self.assertEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(result["decisions"][0]["reason"], "static_admission_failed")
        self.assertIn("Unsupported node declaration", result["decisions"][0]["error"])
        self.assertTrue((self.root / "experiment/rounds/R1/candidate-payload.json").exists())

    async def test_invalid_source_yaml_and_nontext_candidates_are_rejected_durably(self):
        for index, kind, content in (
            (0, "F", "import os\ndef run(params):\n    return {}\n"),
            (1, "S", "nodes: ["),
            (2, "F", None),
        ):
            with self.subTest(kind=kind, content=content):
                action = candidate_action(self.kernel)
                next(a for a in action["patches"]["assets"] if a["kind"] == kind)["content"] = (
                    content
                )
                self.client = ProposalClient([action])
                controller = self.controller()
                controller.root = self.root / ("invalid-" + str(index))
                result = await controller.run(self.kernel)
                self.assertEqual(result["selected"], self.kernel.content_id)
                self.assertEqual(result["decisions"][0]["reason"], "static_admission_failed")

    async def test_unhealthy_validation_never_selects_candidate_or_leaks_judge_error(self):
        self.evaluator = ArtifactEvaluator(fail_validation=True)
        result = await self.controller().run(self.kernel)
        self.assertEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(result["decisions"][0]["reason"], "unhealthy_evaluation")
        self.assertEqual(result["status"], "failed")
        for path in (self.root / "experiment").rglob("*.json"):
            self.assertNotIn("PRIVATE-GOLD-SENTINEL", path.read_text(), str(path))

    async def test_candidate_behavioral_admission_failure_is_retained(self):
        action = candidate_action(self.kernel)
        action["patches"]["assets"][-1]["content"] = "def check(candidate):\n    return False\n"
        self.client = ProposalClient([action])
        result = await self.controller().run(self.kernel)
        self.assertEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(result["decisions"][0]["reason"], "behavioral_admission_failed")
        self.assertTrue((self.root / "experiment/rounds/R1/candidate/manifest.json").exists())

    async def test_changed_evaluator_dataset_backend_reject_resume(self):
        controller = self.controller(spec=replace(self.spec, rounds=0))
        await controller.run(self.kernel)
        self.evaluator = ArtifactEvaluator(gold="changed references")
        with self.assertRaisesRegex(ValueError, "identical"):
            await self.controller(spec=replace(self.spec, rounds=0)).run(self.kernel, resume=True)
        self.evaluator = ArtifactEvaluator()
        self.dataset.suffix = "changed"
        with self.assertRaisesRegex(ValueError, "identical"):
            await self.controller(spec=replace(self.spec, rounds=0)).run(self.kernel, resume=True)
        self.dataset.suffix = ""
        self.backend.profile = replace(self.profile, max_leaf_calls=63)
        with self.assertRaisesRegex(ValueError, "identical"):
            await self.controller(spec=replace(self.spec, rounds=0)).run(self.kernel, resume=True)

    async def test_write_backend_requires_isolated_factory_before_dispatch(self):
        backend = ExecutionBackend(
            ScriptedAgent(), self.tools, self.profile, run_root=self.root / "runs"
        )
        with self.assertRaisesRegex(ValueError, "workspace isolation"):
            await self.controller(backend=backend).run(self.kernel)
        self.assertFalse((self.root / "runs").exists())

    async def test_episode_budget_reserves_once_before_dispatch(self):
        result = await self.controller(spec=replace(self.spec, max_episode_runs=1)).run(self.kernel)
        self.assertEqual(result["status"], "budget_exhausted")
        result2 = await self.controller(spec=replace(self.spec, max_episode_runs=1)).run(
            self.kernel, resume=True
        )
        self.assertEqual(result2, result)
        ledger = read_record(self.root / "experiment/episode-ledger.json")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(self.client.calls, 0)

    async def test_unknown_episode_never_becomes_final_comparison(self):
        self.backend.adapter = ReturnsUnknownAgent()
        controller = self.controller()
        for resume in (False, True):
            result = await controller.run(self.kernel, resume=resume)
            self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.client.calls, 0)
        self.assertFalse(list((self.root / "experiment/episodes").glob("*/episode.json")))
        self.assertFalse((self.root / "experiment/agent-summary.json").exists())
        self.assertFalse((self.root / "experiment/selection.json").exists())

    async def test_unknown_resume_rejects_host_receipt_relocation_before_second_call(self):
        lost = LostReceiptClient()
        self.backend.adapter, self.backend.client = ModelTwiceAgent(), lost
        controller = self.controller()
        self.assertEqual((await controller.run(self.kernel))["status"], "unknown")
        self.assertEqual((await controller.run(self.kernel, resume=True))["status"], "unknown")
        self.assertEqual(lost.calls, 1)
        self.backend.run_root = self.root / "relocated-host-store"
        with self.assertRaisesRegex(ValueError, "identical"):
            await controller.run(self.kernel, resume=True)
        self.assertEqual(lost.calls, 1)
        self.assertFalse(self.backend.run_root.exists())

    async def test_unknown_resume_rejects_derived_receipt_relocation_before_second_call(self):
        lost = LostReceiptClient()
        backend = DerivedReceiptBackend(
            ModelTwiceAgent(), self.tools, self.profile, lost, self.root / "runs"
        )
        controller = self.controller(backend=backend)
        self.assertEqual((await controller.run(self.kernel))["status"], "unknown")
        pins = list((controller.root / "episodes").glob("*/backend-identity.json"))
        self.assertEqual(len(pins), 1)
        self.assertEqual(
            read_record(pins[0])["receipt_store"], str((backend.run_root / "first").resolve())
        )
        self.assertEqual((await controller.run(self.kernel, resume=True))["status"], "unknown")
        self.assertEqual(lost.calls, 1)
        before = backend.identity()
        backend.derived_store = "second"
        self.assertEqual(before, backend.identity())
        with self.assertRaisesRegex(ValueError, "receipt store changed"):
            await controller.run(self.kernel, resume=True)
        self.assertEqual(lost.calls, 1)
        self.assertFalse((backend.run_root / "second").exists())
        self.assertFalse((controller.root / "selection.json").exists())

    async def test_bound_factories_and_primitive_handler_complete_and_resume(self):
        from darwinagent.experiments.episode_experiment import source_identity

        self.assertIsInstance(source_identity(self.evaluator.declaration), str)
        factories = BoundFactories(self.evaluator, self.client)
        self.backend = BoundPrimitiveBackend(
            ScriptedAgent(),
            bound_file_tools(self.root / "host"),
            self.profile,
            run_root=self.root / "runs",
        )
        controller = self.controller()
        controller.evaluator_factory = factories.evaluator_factory
        controller._injected_client = factories.client_factory
        result = await controller.run(self.kernel)
        self.assertEqual(result["status"], "complete")
        self.assertNotEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(await controller.run(self.kernel, resume=True), result)
        self.assertEqual(self.client.calls, 1)
        for sentinel in ("BOUND-FACTORY-PRIVATE-GOLD", "BOUND-HANDLER-PRIVATE-CRITERION"):
            self.assertNotIn(sentinel, json.dumps(self.client.messages))
            for path in controller.root.rglob("*.json"):
                self.assertNotIn(sentinel, path.read_text(), str(path))
        factories.private_reference = "changed-private-reference"
        with self.assertRaisesRegex(ValueError, "identical"):
            await controller.run(self.kernel, resume=True)
        factories.private_reference = "BOUND-FACTORY-PRIVATE-GOLD"
        factories.revision = "changed-config"
        with self.assertRaisesRegex(ValueError, "identical"):
            await controller.run(self.kernel, resume=True)
        factories.revision = "original"
        self.backend.tools["write_file"].handler.__self__.mode = "changed"
        with self.assertRaisesRegex(ValueError, "identical"):
            await controller.run(self.kernel, resume=True)
        self.assertEqual(self.client.calls, 1)

    async def test_static_admission_never_writes_artifacts(self):
        self.backend.admit(self.kernel)
        self.assertFalse((self.root / "host/report.txt").exists())

    async def test_unsafe_isolation_factories_rejected_before_worker_dispatch(self):
        for cls, reason in (
            (SharedWorkspaceBackend, "isolated workspace"),
            (ChangedProfileBackend, "fixed runtime"),
        ):
            with self.subTest(backend=cls.__name__):
                backend = cls(
                    ScriptedAgent(), self.tools, self.profile, run_root=self.root / "runs"
                )
                controller = self.controller(backend=backend)
                controller.root = self.root / cls.__name__
                with self.assertRaisesRegex(ValueError, reason):
                    await controller.run(self.kernel)
        self.assertFalse((self.root / "runs").exists())

    async def test_async_factory_and_training_admission_overlap(self):
        backend = AsyncIsolatedFileBackend(
            ScriptedAgent(), self.tools, self.profile, run_root=self.root / "runs"
        )
        result = await self.controller(
            backend=backend, spec=replace(self.spec, admission=("train",))
        ).run(self.kernel)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["episode_runs"], 8)

    async def test_wiki_query_cannot_retrieve_holdout_gold_or_diagnostics(self):
        self.evaluator = ArtifactEvaluator(holdout_feedback=True)
        self.client = ProposalClient(
            [
                {
                    "action": "query_wiki",
                    "query": {
                        "question": "Read validation originals",
                        "scope": {"case_ids": ["val"]},
                        "view": "raw",
                        "cursor": None,
                    },
                },
                candidate_action(self.kernel),
            ]
        )
        result = await self.controller().run(self.kernel)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.client.calls, 2)
        for path in (self.root / "experiment/optimization").rglob("*.json"):
            self.assertNotIn("PRIVATE-GOLD-SENTINEL", path.read_text())
        self.assertNotIn("PRIVATE-GOLD-SENTINEL", json.dumps(self.client.messages))
        self.assertTrue(
            any(
                "PRIVATE-GOLD-SENTINEL" in p.read_text()
                for p in (self.root / "experiment/episodes").glob("*/evaluation.json")
            )
        )

    async def test_proposal_call_budget_cannot_dispatch_an_extra_request(self):
        from darwinagent.agents.protocol import ProtocolError

        self.client = ProposalClient(
            [
                {
                    "action": "query_wiki",
                    "query": {
                        "question": "Read training",
                        "scope": {"case_ids": ["train"]},
                        "view": "raw",
                        "cursor": None,
                    },
                }
            ]
        )
        controller = self.controller(spec=replace(self.spec, proposal_call_limit=1))
        for resume in (False, True):
            with self.assertRaisesRegex(ProtocolError, "budget exhausted"):
                await controller.run(self.kernel, resume=resume)
        self.assertEqual(self.client.calls, 1)
        self.assertFalse((self.root / "experiment/selection.json").exists())

    async def test_frozen_source_change_rejects_resume(self):
        source = self.root / "host-source.py"
        source.write_text("VALUE = 1\n")
        from darwinagent.runtime.identity import snapshot_files

        controller = self.controller(spec=replace(self.spec, rounds=0))
        controller.frozen.update(snapshot_files([source]))
        await controller.run(self.kernel)
        source.write_text("VALUE = 2\n")
        with self.assertRaisesRegex(ValueError, "Frozen engineering"):
            await controller.run(self.kernel, resume=True)
        replacement = self.controller(spec=replace(self.spec, rounds=0))
        replacement.frozen.update(snapshot_files([source]))
        with self.assertRaisesRegex(ValueError, "identical"):
            await replacement.run(self.kernel, resume=True)

    async def test_wiki_regroup_has_durable_global_model_budget(self):
        self.client = ProposalClient(
            [
                {
                    "action": "query_wiki",
                    "query": {
                        "question": "Inspect all training originals",
                        "scope": {},
                        "view": "regroup",
                        "cursor": None,
                    },
                },
                {"facts": [], "hypotheses": [], "support": [], "refutation": [], "uncertainty": []},
                {"action": "no_change", "reason": "Need more evidence"},
            ]
        )
        controller = self.controller()
        controller.wiki_call_limit = 1
        result = await controller.run(self.kernel)
        self.assertEqual(result["selected"], self.kernel.content_id)
        self.assertEqual(self.client.calls, 3)
        counter = json.loads((self.root / "experiment/wiki-calls.json").read_text())
        self.assertEqual(counter["reserved_calls"], 1)
        resumed = await controller.run(self.kernel, resume=True)
        self.assertEqual(result, resumed)
        self.assertEqual(self.client.calls, 3)


class EpisodeSelectionTests(unittest.TestCase):
    def rows(self, **changes):
        return {
            "v": {
                "success": 1,
                "content": 1,
                "healthy": True,
                "correction_turns": 2,
                "leaf_calls": 5,
                "execution_tokens": 20,
                **changes,
            }
        }

    def test_quality_components_and_complete_pairs(self):
        cases = [
            (self.rows(success=2, content=0), "quality_regression"),
            (self.rows(success=0, content=2), "quality_regression"),
            ({}, "incomplete_pairs"),
            (self.rows(healthy=False), "unhealthy_evaluation"),
        ]
        for candidate, reason in cases:
            with self.subTest(reason=reason):
                result = select_episode_candidate(self.rows(), candidate)
                self.assertFalse(result["accepted"])
                self.assertEqual(result["reason"], reason)

    def test_equal_quality_efficiency_order_and_unknown(self):
        self.assertTrue(
            select_episode_candidate(self.rows(), self.rows(correction_turns=1, leaf_calls=999))[
                "accepted"
            ]
        )
        self.assertTrue(select_episode_candidate(self.rows(), self.rows(leaf_calls=4))["accepted"])
        self.assertTrue(
            select_episode_candidate(self.rows(), self.rows(execution_tokens=19))["accepted"]
        )
        self.assertFalse(select_episode_candidate(self.rows(), self.rows())["accepted"])
        self.assertFalse(
            select_episode_candidate(
                self.rows(), self.rows(correction_turns=1, execution_tokens=None)
            )["accepted"]
        )

    def test_missing_worker_usage_does_not_become_measured_zero(self):
        episode = EpisodeResult(
            "completed",
            events=({"kind": "model", "usage": {}},),
            usage={"model_calls": 1, "leaf_calls": 2, "prompt_tokens": 0, "completion_tokens": 0},
        )
        metrics = episode_metrics(episode, EpisodeEvaluation(1, 1, correction_turns=0))
        self.assertIsNone(metrics["execution_tokens"])
        self.assertEqual(metrics["leaf_calls"], 2)

    def test_overlapping_splits_rejected(self):
        with self.assertRaises(ValueError):
            AgentExperimentSpec(("same",), ("same",), ("a",))

    def test_quality_metrics_reject_invalid_measurements(self):
        for success, content in ((-1, 1), (2, 1), (1, -1), (1, float("nan"))):
            with self.assertRaises(ValueError):
                EpisodeEvaluation(success, content)

    def test_patch_decoder_cannot_change_host_profile(self):
        with tempfile.TemporaryDirectory() as root:
            profile = ExecutionProfile("files", ("read_file", "write_file"))
            kernel = bundle(Path(root) / "bundle", profile)
            action = candidate_action(kernel)
            action["patches"]["profile"] = {"max_leaf_calls": 999}
            with self.assertRaisesRegex(ValueError, "host"):
                decode_candidate(action, kernel, profile, {})
            patch = {
                "op": "upsert",
                "asset": {**kernel.get("worker").to_dict(), "content": "New $instruction"},
            }
            candidate = decode_candidate({"patches": [patch]}, kernel, profile, {})
            self.assertNotEqual(candidate.content_id, kernel.content_id)

    def test_legacy_default_is_explicit_compatibility_backend(self):
        with tempfile.TemporaryDirectory() as root:
            runner = ExperimentRunner(None, None, Config(), RunConfig(), None, root)
            self.assertIsInstance(runner.backend, GraphQACompatibilityBackend)
