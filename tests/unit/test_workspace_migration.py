import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.runtime.migration import (
    export_legacy_run,
    export_run,
    export_workspace,
    import_bundle,
    import_run,
)
from darwinagent.runtime.workspace import Workspace


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_native_roundtrip_preserves_objects_selections_and_progress(self):
        source = Workspace(self.root / "source")
        ref = source.put_bytes(b"original\r\n")
        source.add_provenance(ref, {"model": "old"})
        source.add_reference("answer-q1", "graph", ref)
        source.create_branch("main", working=ref)
        attempt = source.start_attempt("q1", "answer", progress={"next": "review"})
        source.finish_attempt(attempt, "interrupted")
        report = export_workspace(source, self.root / "bundle")
        self.assertEqual("complete", report["status"])
        target = Workspace(self.root / "target")
        report = import_bundle(self.root / "bundle", target)
        self.assertEqual("complete", report["status"])
        self.assertEqual(b"original\r\n", target.read_bytes(ref))
        self.assertEqual(ref, target.branch("main")["working"])
        self.assertEqual("review", target.attempts("q1")[0]["progress"]["next"])

    def test_corrupt_bundle_does_not_delete_target_success_and_good_files_import(self):
        source = Workspace(self.root / "source")
        good = source.put_bytes(b"good")
        bad = source.put_bytes(b"bad")
        export_workspace(source, self.root / "bundle")
        (self.root / "bundle" / "objects" / bad).write_bytes(b"broken")
        target = Workspace(self.root / "target")
        existing = target.put_bytes(b"target-success")
        report = import_bundle(self.root / "bundle", target)
        self.assertEqual("partial", report["status"])
        self.assertEqual(b"target-success", target.read_bytes(existing))
        self.assertEqual(b"good", target.read_bytes(good))
        self.assertTrue(report["not_imported"])
        with self.assertRaises(KeyError):
            target.read_bytes(bad)

    def test_bad_manifest_path_fails_before_mutation(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "format": "darwinagent-workspace-1",
                    "objects": [{"id": "a" * 64, "path": "../outside", "format": "bytes"}],
                    "records": {},
                }
            )
        )
        target = Workspace(self.root / "target")
        ref = target.put_bytes(b"target")
        with self.assertRaises(ValueError):
            import_bundle(bundle, target)
        self.assertEqual(b"target", target.read_bytes(ref))
        self.assertEqual(1, len(target.list_objects()))

    def test_legacy_scans_all_rounds_and_external_dependencies_without_stop_or_keys(self):
        source = self.root / "legacy"
        (source / "train" / "R3").mkdir(parents=True)
        external = self.root / "graph.json"
        external.write_bytes(b'{ "nodes": [] }\r\n')
        (source / "train" / "R3" / "answer.json").write_text(
            json.dumps({"graph": str(external), "missing": "/missing/source.txt"})
        )
        (source / "STOP").write_text("stop")
        (source / ".env").write_text("API_KEY=secret")
        (source / "credentials.json").write_text('{"api_key":"secret"}')
        report = export_legacy_run(source, self.root / "bundle")
        self.assertEqual("partial", report["status"])
        self.assertTrue(report["missing"])
        manifest = json.loads((self.root / "bundle" / "manifest.json").read_text())
        paths = [row["logical_path"] for row in manifest["objects"]]
        self.assertIn("train/R3/answer.json", paths)
        self.assertFalse(any("STOP" in p or ".env" in p or "credentials" in p for p in paths))
        target = Workspace(self.root / "target")
        imported = import_bundle(self.root / "bundle", target)
        self.assertEqual("partial", imported["status"])
        self.assertTrue(
            any(
                target.read_bytes(row["id"]) == external.read_bytes()
                for row in target.list_objects()
            )
        )

    def test_branch_collision_does_not_overwrite_human_choice(self):
        source = Workspace(self.root / "source")
        source.create_branch("main", working="source")
        export_workspace(source, self.root / "bundle")
        target = Workspace(self.root / "target")
        target.create_branch("main", working="target")
        report = import_bundle(self.root / "bundle", target)
        self.assertEqual("target", target.branch("main")["working"])
        self.assertEqual("partial", report["status"])

    def test_invalid_evidence_does_not_import_dependent_progress(self):
        source = Workspace(self.root / "source")
        ref = source.put_json({"answer": "saved"})
        source.add_reference("q1", "answer", ref)
        attempt = source.start_attempt("q1", "answer")
        source.finish_attempt(attempt, "succeeded", result_ref=ref)
        export_workspace(source, self.root / "bundle")
        (self.root / "bundle" / "objects" / ref).write_bytes(b"corrupt")
        target = Workspace(self.root / "target")
        report = import_bundle(self.root / "bundle", target)
        self.assertEqual("partial", report["status"])
        self.assertEqual([], target.references("q1"))
        self.assertEqual([], target.attempts("q1"))

    def test_inflight_requests_and_attempts_import_as_interrupted(self):
        source = Workspace(self.root / "source")
        request = source.prepare_request({"messages": ["hello"]})
        source.submit_request(request)
        source.start_attempt("q1", "answer")
        export_workspace(source, self.root / "bundle")
        target = Workspace(self.root / "target")
        import_bundle(self.root / "bundle", target)
        self.assertEqual("unknown", target.request(request)["status"])
        self.assertEqual("interrupted", target.attempts("q1")[0]["status"])
        self.assertEqual("submitted", source.request(request)["status"])

    def test_export_failure_preserves_existing_destination(self):
        source = Workspace(self.root / "source")
        destination = self.root / "existing"
        destination.mkdir()
        marker = destination / "result.txt"
        marker.write_bytes(b"completed")
        with self.assertRaises(ValueError):
            export_workspace(source, destination)
        self.assertEqual(b"completed", marker.read_bytes())

    def test_complete_run_restores_wiki_steps_and_relocates_answer_graph(self):
        source = self.root / "run-source"
        workspace = Workspace(source / "workspace")
        workspace.create_branch("main", working="manual")
        graph = source / "generation" / "c" / "graph.json"
        graph.parent.mkdir(parents=True)
        graph.write_bytes(b'{"nodes": []}')
        answer = graph.parent / "branches" / "main" / "answers" / "q.json"
        answer.parent.mkdir(parents=True)
        original = json.dumps({"graph_path": str(graph), "identity": "old-producer"}).encode()
        answer.write_bytes(original)
        steps = source / "generation" / "c" / "steps" / "000000.json"
        steps.parent.mkdir()
        steps.write_bytes(b'{"state":"responded","response":"saved"}')
        wiki = source / "optimization" / "evidence" / "jobs" / "job.json"
        wiki.parent.mkdir(parents=True)
        wiki.write_bytes(b'{"completed":{"0":"saved"}}')
        (source / "STOP").write_text("stop")
        export_run(source, self.root / "full-bundle")
        target = self.root / "run-target"
        report = import_run(self.root / "full-bundle", target)
        self.assertEqual("complete", report["status"])
        current_answer = target / answer.relative_to(source)
        current = json.loads(current_answer.read_bytes())
        self.assertEqual(str((target / graph.relative_to(source)).resolve()), current["graph_path"])
        self.assertEqual("old-producer", current["identity"])
        self.assertEqual(wiki.read_bytes(), (target / wiki.relative_to(source)).read_bytes())
        self.assertEqual(steps.read_bytes(), (target / steps.relative_to(source)).read_bytes())
        self.assertFalse((target / "STOP").exists())
        imported = Workspace(target / "workspace")
        self.assertTrue(
            any(imported.read_bytes(row["id"]) == original for row in imported.list_objects())
        )
        self.assertEqual("manual", imported.branch("main")["working"])

    def test_complete_run_conflict_is_isolated_and_original_target_survives(self):
        source = self.root / "source-run"
        source.mkdir()
        (source / "result.json").write_text('{"result":"new"}')
        export_run(source, self.root / "full-bundle")
        target = self.root / "target-run"
        target.mkdir()
        (target / "result.json").write_text('{"result":"existing"}')
        report = import_run(self.root / "full-bundle", target)
        self.assertEqual("partial", report["status"])
        self.assertEqual('{"result":"existing"}', (target / "result.json").read_text())
        self.assertTrue(report["isolated"])
        self.assertTrue(Path(report["isolated"][0]["isolated_path"]).exists())

    def test_existing_different_graph_cannot_rebind_an_imported_old_answer(self):
        source = self.root / "graph-source"
        source.mkdir()
        graph = source / "graph.json"
        graph.write_text('{"old":true}')
        (source / "answer.json").write_text(json.dumps({"graph_path": str(graph)}))
        export_run(source, self.root / "graph-bundle")
        target = self.root / "graph-target"
        target.mkdir()
        (target / "graph.json").write_text('{"new":true}')
        report = import_run(self.root / "graph-bundle", target)
        self.assertEqual("partial", report["status"])
        answer = json.loads((target / "answer.json").read_text())
        self.assertEqual('{"old":true}', Path(answer["graph_path"]).read_text())
        self.assertEqual('{"new":true}', (target / "graph.json").read_text())

    def test_registered_asset_version_branch_survives_public_transfer_and_proposal(self):
        from types import SimpleNamespace

        from darwinagent.config import RunConfig
        from darwinagent.experiments.proposal import ProposalGenerator
        from darwinagent.experiments.rounds import _remember_bundle
        from darwinagent.experiments.selected_operations import _bundle
        from darwinagent.kernel.assets import KernelAssets
        from darwinagent.llm.recorded import RecordedClient
        from darwinagent.runtime.execution import ExecutionSelection
        from tests.support.device import case, spec

        source = self.root / "candidate-source"
        task = spec(source / "baseline")
        external = self.root / "external-candidate"
        candidate = KernelAssets(task.bundle.assets.assets, {"kind": "proposal"}).export(external)
        workspace = Workspace(source / "workspace")
        _remember_bundle(workspace, task.bundle)
        _remember_bundle(workspace, candidate)
        workspace.create_branch("main", adopted=task.bundle.version, working=candidate.version)
        self.assertEqual("complete", export_run(source, self.root / "candidate-bundle")["status"])
        source.rename(self.root / "old-source-unavailable")
        external.rename(self.root / "old-candidate-unavailable")
        target = self.root / "candidate-target"
        report = import_run(self.root / "candidate-bundle", target)
        self.assertEqual("complete", report["status"], report)
        imported = Workspace(target / "workspace")
        branch = imported.branch("main")
        self.assertEqual(task.bundle.version, branch["adopted"])
        self.assertEqual(candidate.version, branch["working"])
        selected = _bundle(
            SimpleNamespace(root=target),
            SimpleNamespace(bundle=None),
            ExecutionSelection(),
            imported,
        )
        self.assertEqual(candidate.version, selected.version)
        self.assertTrue(selected.root.is_relative_to(target.resolve()))
        transport = RecordedClient(
            {"proposal": [{"action": "no_change", "reason": "retained candidate"}]}
        )
        result = asyncio.run(
            ProposalGenerator().propose(
                selected, case(), {}, transport, RunConfig(), target / "proposal.json"
            )
        )
        self.assertEqual((), result)
        payload = json.loads(transport.calls[0]["messages"][1]["content"])
        self.assertEqual(candidate.version, payload["base_version"])

    def test_low_level_branch_identifier_is_not_assumed_to_be_content_hash(self):
        source = Workspace(self.root / "version-source")
        selected_version = "a" * 64
        source.create_branch("main", working=selected_version)
        export_workspace(source, self.root / "version-bundle")
        target = Workspace(self.root / "version-target")
        report = import_bundle(self.root / "version-bundle", target)
        self.assertEqual("complete", report["status"])
        self.assertEqual(selected_version, target.branch("main")["working"])

    def test_registered_bundle_dependency_corruption_reports_partial_without_dropping_branch(self):
        from darwinagent.experiments.rounds import _remember_bundle
        from tests.support.device import spec

        source = self.root / "broken-version-source"
        task = spec(source / "baseline")
        workspace = Workspace(source / "workspace")
        _remember_bundle(workspace, task.bundle)
        workspace.create_branch("main", working=task.bundle.version)
        asset = task.bundle.path("device_lookup")
        asset.write_bytes(b"corrupt source")
        exported = export_run(source, self.root / "broken-version-bundle")
        self.assertEqual("partial", exported["status"])
        self.assertTrue(exported["progress"]["missing"])
        target = self.root / "broken-version-target"
        imported = import_run(self.root / "broken-version-bundle", target)
        self.assertEqual("partial", imported["status"])
        self.assertEqual(
            task.bundle.version, Workspace(target / "workspace").branch("main")["working"]
        )
        self.assertTrue(any("registration" in gap for gap in imported["isolated"]))

    def test_registered_bundle_is_coherent_when_target_old_layout_collides(self):
        from types import SimpleNamespace

        from darwinagent.experiments.rounds import _remember_bundle
        from darwinagent.experiments.selected_operations import _bundle
        from darwinagent.kernel import KernelBundle
        from darwinagent.kernel.assets import KernelAssets
        from darwinagent.runtime.execution import ExecutionSelection
        from tests.support.device import spec

        source = self.root / "layout-source"
        task = spec(source / "baseline")
        workspace = Workspace(source / "workspace")
        _remember_bundle(workspace, task.bundle)
        workspace.create_branch("main", working=task.bundle.version)
        export_run(source, self.root / "layout-bundle")
        target = self.root / "layout-target"
        old = KernelAssets(task.bundle.assets.assets, {"kind": "target-old"}).export(
            target / "baseline"
        )
        report = import_run(self.root / "layout-bundle", target)
        self.assertEqual("partial", report["status"])
        self.assertEqual(old.version, KernelBundle(target / "baseline").version)
        selected = _bundle(
            SimpleNamespace(root=target),
            SimpleNamespace(bundle=None),
            ExecutionSelection(),
            Workspace(target / "workspace"),
        )
        self.assertEqual(task.bundle.version, selected.version)
        selected.verify()

    def test_independent_receipt_and_explicit_replacement_links_survive_transfer(self):
        source = self.root / "requests-source"
        workspace = Workspace(source / "workspace")
        request = workspace.prepare_request({"messages": ["receipt gap"]})
        workspace.submit_request(request)
        response = workspace.put_json({"content": "saved before state commit"})
        workspace.write_receipt(request, response)
        unknown = workspace.prepare_request({"messages": ["unknown"]})
        workspace.submit_request(unknown)
        replacement = workspace.retry_request(unknown)
        export_run(source, self.root / "requests-bundle")
        target = self.root / "requests-target"
        report = import_run(self.root / "requests-bundle", target)
        self.assertEqual("complete", report["status"], report)
        imported = Workspace(target / "workspace")
        self.assertEqual(replacement, imported.replacement_for(unknown)["id"])
        self.assertEqual("prepared", imported.replacement_for(unknown)["status"])
        imported.recover_requests()
        self.assertEqual("responded", imported.request(request)["status"])
        self.assertEqual(response, imported.request(request)["response_ref"])
