import tempfile
import unittest
from pathlib import Path

from darwinagent.kernel.assets import KernelAssets
from darwinagent.runtime.workspace import SelectionConflict, Workspace
from tests.support.device import spec


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))

    def test_original_bytes_and_source_independence(self):
        raw = b'{ "a": 1 }\r\n'
        ref = self.workspace.put_bytes(raw, format="json-original")
        self.workspace.add_provenance(ref, {"model": "first"})
        self.workspace.add_provenance(ref, {"model": "second"})
        self.assertEqual(ref, self.workspace.put_bytes(raw, format="json-original"))
        self.assertEqual(raw, self.workspace.read_bytes(ref))
        self.assertEqual(2, len(self.workspace.provenance(ref)))
        self.assertEqual(
            self.workspace.put_json({"a": 1, "b": 2}), self.workspace.put_json({"b": 2, "a": 1})
        )

    def test_corruption_is_detected_without_overwriting(self):
        ref = self.workspace.put_bytes(b"original")
        self.workspace.object_path(ref).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.workspace.read_bytes(ref)
        with self.assertRaises(ValueError):
            self.workspace.put_bytes(b"original")
        self.assertEqual(b"corrupt", self.workspace.object_path(ref).read_bytes())

    def test_old_automatic_selection_cannot_overwrite_human(self):
        self.workspace.create_branch("main", working="old", adopted="old")
        self.workspace.select_branch("main", working="human", expected_revision=0, source="human")
        with self.assertRaises(SelectionConflict):
            self.workspace.select_branch("main", working="auto", expected_revision=0)
        self.assertEqual("human", self.workspace.branch("main")["working"])
        self.assertEqual("old", self.workspace.branch("main")["adopted"])
        self.workspace.create_branch("fork", parent="main")
        self.workspace.select_branch("fork", working="forked", expected_revision=0)
        self.assertEqual("human", self.workspace.branch("main")["working"])

    def test_receipt_recovers_response_before_state_commit(self):
        request = self.workspace.prepare_request({"messages": ["hello"]})
        self.workspace.submit_request(request)
        response = self.workspace.put_json({"text": "saved"})
        self.workspace.write_receipt(request, response, {"reported_model": "actual"})
        reopened = Workspace(Path(self.tmp.name))
        reopened.recover_requests()
        row = reopened.request(request)
        self.assertEqual("responded", row["status"])
        self.assertEqual(response, row["response_ref"])
        self.assertEqual("actual", row["receipt"]["reported_model"])
        other = reopened.prepare_request({"messages": ["unknown"]})
        reopened.submit_request(other)
        reopened.recover_requests()
        self.assertEqual("unknown", reopened.request(other)["status"])

    def test_attempt_history_and_progress_survive_failure(self):
        first = self.workspace.start_attempt("q1", "answer")
        self.workspace.save_progress(first, {"next_step": "review", "visible": [1]})
        self.workspace.finish_attempt(first, "failed")
        second = self.workspace.start_attempt("q1", "answer")
        self.assertNotEqual(first, second)
        rows = self.workspace.attempts("q1", "answer")
        self.assertEqual(["failed", "running"], [r["status"] for r in rows])
        self.assertEqual("review", rows[0]["progress"]["next_step"])

    def test_metadata_secrets_are_rejected(self):
        with self.assertRaises(ValueError):
            self.workspace.prepare_request({"connection": {"api_key": "sensitive"}})
        with self.assertRaises(ValueError):
            self.workspace.append_event("connection", {"password": "sensitive"})

    def test_asset_content_identity_does_not_change_historical_manifest(self):
        original = spec(Path(self.tmp.name) / "seed").bundle.assets
        first = KernelAssets(original.assets, {"model": "first"})
        second = KernelAssets(original.assets, {"model": "second"})
        before = first.manifest()
        self.assertEqual(first.content_id, second.content_id)
        self.assertNotEqual(first.manifest()["version"], second.manifest()["version"])
        self.assertEqual(before, first.manifest())
        self.assertNotIn("content_id", before)
