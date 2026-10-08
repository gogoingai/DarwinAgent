"""Daily smoke retains native tool/model receipts; strict legacy smoke stays temporary."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import QuestionInput
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.kernel import KernelBundle
from darwinagent.kernel.assets import KernelAssets
from darwinagent.runtime.artifacts import digest
from darwinagent.runtime.execution import ExecutionSelection
from darwinagent.runtime.steps import UnknownRequest
from darwinagent.runtime.workspace import Workspace
from scripts.smoke_live_loop import (
    AuditTransport,
    FixtureAdapter,
    evaluator_factory,
    prepare_fixture,
    prepared_graph,
)


class DurableSmokeTests(unittest.TestCase):
    def fixture(self, root, execution):
        cases, oracle, seed, spec = prepare_fixture(root, "replay")
        cfg = Config(model_strong="glm-5.3-flash", work_dir=root / "transport")
        transport = AuditTransport(root, cfg, replay=True)
        runner = ExperimentRunner(
            FixtureAdapter(cases),
            evaluator_factory(oracle),
            cfg,
            RunConfig(concurrency=1, protocol_attempts=1, answer_attempts=1),
            AdoptionPolicy("field_exact", ()),
            root,
            client_factory=transport.for_stage,
            snapshot_root=root / "snapshots",
            graph_builder=prepared_graph,
        )
        runner.execution = execution
        return runner, cases[:1], spec.with_bundle(KernelBundle(seed)), transport

    def run_smoke(self, runner, cases, spec):
        with (
            mock.patch("httpx.AsyncClient.send", side_effect=AssertionError("Offline only")),
            mock.patch(
                "darwinagent.vector.load_embedder", side_effect=AssertionError("No embedding model")
            ),
        ):
            return asyncio.run(runner._smoke_gate(cases, spec, questions_per_case=1))

    def test_daily_smoke_keeps_native_steps_and_continue_adds_no_calls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner, cases, spec, transport = self.fixture(root, ExecutionSelection())
            self.assertIsNone(self.run_smoke(runner, cases, spec))
            smoke = root / "smoke" / spec.bundle.version
            self.assertTrue((smoke / "identity.json").exists())
            rows = [
                json.loads(p.read_text())
                for p in smoke.rglob("steps/**/*.json")
                if p.name != "progress.json"
            ]
            self.assertTrue(rows)
            self.assertEqual({r["kind"] for r in rows}, {"tool", "model"})
            self.assertTrue(all(r["state"] == "responded" for r in rows))
            self.assertTrue(all(digest(r["response"]) == r["response_digest"] for r in rows))
            workspace = Workspace(root / "workspace")
            self.assertTrue(
                all(workspace.request(r["request_id"])["status"] == "responded" for r in rows)
            )
            self.assertTrue(
                all((workspace.receipts / (r["request_id"] + ".json")).exists() for r in rows)
            )
            calls = len(transport.calls)
            self.assertIsNone(self.run_smoke(runner, cases, spec))
            self.assertEqual(len(transport.calls), calls)
            self.assertTrue(list(smoke.rglob("result.json")))

    def test_scope_is_filtered_before_sampling_and_excludes_other_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner, cases, spec, transport = self.fixture(
                root, ExecutionSelection(case_ids=("train-a",), question_ids=("q8",))
            )
            case = replace(
                cases[0],
                questions=tuple(
                    QuestionInput("q" + str(i), cases[0].questions[0].text, {"serial": "D-17"})
                    for i in range(1, 9)
                ),
            )
            excluded = replace(case, id="outside-scope")
            self.assertIsNone(self.run_smoke(runner, (excluded, case), spec))
            result = json.loads(next((root / "smoke").rglob("result.json")).read_text())
            self.assertEqual([a["question_id"] for a in result["answers"]], ["q8"])
            self.assertEqual(result["case_id"], "train-a")
            self.assertEqual(len(transport.calls), 4)

    def test_new_bundle_gets_separate_smoke_answers_and_native_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner, cases, spec, transport = self.fixture(root, ExecutionSelection())
            self.assertIsNone(self.run_smoke(runner, cases, spec))
            assets = tuple(
                replace(a, content=a.content + "\nfixture_format_v1") if a.role == "answer" else a
                for a in spec.bundle.assets.assets
            )
            candidate = KernelAssets(assets).export(root / "candidate")
            self.assertIsNone(self.run_smoke(runner, cases, spec.with_bundle(candidate)))
            self.assertEqual(len(transport.calls), 8)
            for bundle in (spec.bundle, candidate):
                result = json.loads(
                    next((root / "smoke" / bundle.version).rglob("result.json")).read_text()
                )
                self.assertEqual(result["asset_version"], bundle.version)
                self.assertTrue(
                    all(p["asset_version"] == bundle.version for p in result["answer_provenance"])
                )

    def test_receipt_only_interruption_recovers_without_repeating_answer_or_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner, cases, spec, transport = self.fixture(root, ExecutionSelection())
            original = Workspace.record_response
            interrupted = False

            def record_then_interrupt(workspace, request_id, response, **kwargs):
                nonlocal interrupted
                original(workspace, request_id, response, **kwargs)
                if not interrupted and response.get("role") == "answer":
                    interrupted = True
                    raise RuntimeError("Interrupted after durable receipt before step response")

            with mock.patch.object(Workspace, "record_response", record_then_interrupt):
                with self.assertRaises(UnknownRequest):
                    self.run_smoke(runner, cases, spec)
            before = [c["request"]["role"] for c in transport.calls]
            self.assertEqual(before, ["tools", "tools", "answer"])
            self.assertIsNone(self.run_smoke(runner, cases, spec))
            self.assertEqual([c["request"]["role"] for c in transport.calls], before + ["review"])
            rows = [
                json.loads(p.read_text())
                for p in (root / "smoke").rglob("steps/**/*.json")
                if p.name != "progress.json"
            ]
            self.assertTrue(all(r["state"] == "responded" for r in rows))

    def test_unknown_stays_blocked_until_explicit_retry_preserving_old_submission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner, cases, spec, transport = self.fixture(root, ExecutionSelection())
            original = transport.recorded_reply

            def timeout_answer(stage, request):
                if request["role"] == "answer":
                    raise TimeoutError("synthetic response loss")
                return original(stage, request)

            with mock.patch.object(transport, "recorded_reply", timeout_answer):
                with self.assertRaises(UnknownRequest):
                    self.run_smoke(runner, cases, spec)
            step = next(
                p
                for p in (root / "smoke").rglob("steps/**/*.json")
                if p.name != "progress.json"
                and json.loads(p.read_text()).get("input", {}).get("role") == "answer"
            )
            old = json.loads(step.read_text())
            self.assertEqual(old["state"], "submitted")
            calls = len(transport.calls)
            with self.assertRaises(UnknownRequest):
                self.run_smoke(runner, cases, spec)
            self.assertEqual(len(transport.calls), calls)
            workspace = Workspace(root / "workspace")
            workspace.recover_requests()
            workspace.retry_request(
                old["request_id"], reason="Authorized test retry of synthetic interrupted request"
            )
            self.assertIsNone(self.run_smoke(runner, cases, spec))
            fresh = json.loads(step.read_text())
            self.assertEqual(fresh["state"], "responded")
            self.assertIn(old["request_id"], fresh["previous_requests"])
            self.assertNotEqual(fresh["request_id"], old["request_id"])
            self.assertEqual(workspace.request(old["request_id"])["status"], "abandoned")
            self.assertEqual(
                [c["request"]["role"] for c in transport.calls[calls:]], ["answer", "review"]
            )

    def test_explicit_strict_and_unspecified_execution_keep_legacy_smoke(self):
        for execution in (None, ExecutionSelection(strict=True)):
            with self.subTest(execution=execution), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                runner, cases, spec, transport = self.fixture(root, execution)
                self.assertIsNone(self.run_smoke(runner, cases, spec))
                self.assertFalse((root / "smoke").exists())
                self.assertIsNone(self.run_smoke(runner, cases, spec))
                self.assertEqual(len(transport.calls), 8)


if __name__ == "__main__":
    unittest.main()
