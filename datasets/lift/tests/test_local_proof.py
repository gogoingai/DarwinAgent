"""Historical settlement proofs preserve every journal, mutation and quarantine guard."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.runtime.artifacts import atomic_json, digest
from datasets.lift.scheduling import _local_proof


class LocalProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.shared = SimpleNamespace(root=self.root / "shared")
        self.shared.root.mkdir()
        self.run = self.root / "old"
        self.run.mkdir()
        atomic_json(self.run / "integration-identity.json", {"run_id": "old"})
        self.step = self.run / "sessions/worker/episodes/turn-0/steps/000000.json"
        response = {"output": {"content": "原始结果"}}
        atomic_json(
            self.step,
            {
                "kind": "tool",
                "state": "responded",
                "response": response,
                "response_digest": digest(response),
            },
        )

    def proof(self):
        return _local_proof(self.shared, [{"run_id": "old"}], {"old": str(self.run)}, enrolled={})

    def test_proof_contains_exact_original_file_bytes(self):
        roots, proof = self.proof()
        self.assertEqual(roots, {"old": str(self.run.resolve())})
        expected = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [self.run / "integration-identity.json", self.step]
        }
        self.assertEqual(proof, expected)

    def test_same_size_and_timestamp_rewrite_changes_the_proof(self):
        _, before = self.proof()
        stat = self.step.stat()
        raw = self.step.read_bytes()
        modified = raw.replace("原始结果".encode(), "其他结果".encode())
        self.assertEqual(len(raw), len(modified))
        row = json.loads(modified)
        row["response_digest"] = digest(row["response"])
        atomic_json(self.step, row)
        os.utime(self.step, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        _, after = self.proof()
        self.assertNotEqual(after, before)

    def test_added_valid_step_changes_proof_and_unknown_write_is_rejected(self):
        _, before = self.proof()
        other = self.step.with_name("000001.json")
        atomic_json(other, json.loads(self.step.read_text()))
        self.assertNotEqual(self.proof()[1], before)
        atomic_json(other, {"kind": "tool", "state": "submitted"})
        with self.assertRaisesRegex(ValueError, "Unknown local"):
            self.proof()

    def test_symlinks_anywhere_in_old_run_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.run / "unrelated-artifacts/link"
        link.parent.mkdir()
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.proof()

    def test_trace_errors_in_unrelated_subtrees_are_not_ignored(self):
        atomic_json(self.run / "other/trace-errors/failure.json", {"error": "retained"})
        with self.assertRaisesRegex(ValueError, "trace failure"):
            self.proof()

    def test_only_exact_scheduling_prepared_model_is_eligible(self):
        other = self.step.with_name("000001.json")
        atomic_json(
            other,
            {
                "kind": "model",
                "state": "prepared",
                "pending_budget": "Native scheduling paused: unknown",
            },
        )
        self.assertIn(str(other), self.proof()[1])
        for row in [
            {"kind": "tool", "state": "prepared"},
            {"kind": "model", "state": "unknown"},
            {"kind": "model", "state": "prepared", "pending_budget": "other"},
        ]:
            atomic_json(other, row)
            with self.assertRaises(ValueError):
                self.proof()

    def test_response_digest_tampering_is_rejected(self):
        row = json.loads(self.step.read_text())
        row["response"]["output"]["content"] = "forged"
        atomic_json(self.step, row)
        with self.assertRaisesRegex(ValueError, "digest changed"):
            self.proof()
