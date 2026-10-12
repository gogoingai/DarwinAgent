"""Offline trajectory role audits against production shared/session transports."""

import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.full_transport import AMENDMENT, FullSharedTransport
from datasets.lift.transport import SessionTransport, SharedTransport
from tests.support.full_lift_transport import FakeLLMClient


class FullTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shared_root = self.root / "shared"
        self.old = FakeLLMClient(self.root)
        SharedTransport(self.old, self.shared_root, worker_limit=None)
        atomic_json(self.root / "http-attempts.json", {"attempts": 307})
        atomic_json(
            self.shared_root / "requests/original.json",
            {
                "status": "completed",
                "actual_http_attempts": 307,
                "session": "historic-judge",
                "run_id": "original-run",
                "role": "judge",
                "output": "old evidence",
            },
        )
        atomic_json(self.shared_root / "worker-calls.json", {"historic-worker": 8})
        self.identity = {
            "run_id": "fresh-full",
            "source_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        }
        self.client = FakeLLMClient(self.root, full=True)
        self.original_bytes = (self.shared_root / "policy.json").read_bytes()
        self.receipt_bytes = (self.shared_root / "requests/original.json").read_bytes()

    def full(self, *, enroll=True, client=None, identity=None):
        return FullSharedTransport(
            client or self.client,
            self.shared_root,
            worker_limit=None,
            enroll_role=enroll,
            experiment_identity=identity or self.identity,
        )

    async def test_enroll_production_session_null_caps_preserves_307_and_old_policy(self):
        shared = self.full()
        self.assertEqual(self.client.http_attempts(), 307)
        self.assertEqual(shared.cfg.role_tiers["trajectory_judge"], "strong")
        self.assertIn("trajectory_role_amendment_sha256", shared.identity())
        session = shared.scoped("fresh:scenario:q1:trajectory", "trajectory_judge")
        self.assertIsInstance(session, SessionTransport)
        await session.chat(role="trajectory_judge", messages=[])
        self.assertEqual(self.client.http_attempts(), 308)
        self.assertEqual(self.client.calls[0]["max_tokens"], 16384)
        self.assertEqual(self.client.calls[0]["temperature"], 0)
        self.assertEqual((self.shared_root / "policy.json").read_bytes(), self.original_bytes)
        self.assertEqual(
            (self.shared_root / "requests/original.json").read_bytes(), self.receipt_bytes
        )
        legacy = SharedTransport(self.old, self.shared_root, worker_limit=None)
        legacy.check()
        self.full(enroll=False).check()
        self.assertEqual(
            json.loads((self.shared_root / "worker-calls.json").read_text()), {"historic-worker": 8}
        )

    def test_explicit_enrollment_and_required_frozen_identity(self):
        with self.assertRaisesRegex(ValueError, "Explicit"):
            self.full(enroll=False)
        with self.assertRaisesRegex(ValueError, "identity"):
            FullSharedTransport(self.client, self.shared_root, worker_limit=None, enroll_role=True)
        self.assertFalse((self.shared_root / AMENDMENT).exists())

    def test_old_resumption_label_and_generation_drift_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "fresh run_id"):
            self.full(identity=dict(self.identity, run_id="original-run"))
        self.assertFalse((self.shared_root / AMENDMENT).exists())
        shared = self.full()
        shared.generation_max_tokens = 4096
        with self.assertRaisesRegex(ValueError, "generation policy"):
            shared.check()

    def test_replay_is_idempotent_and_source_changes_require_fresh_run(self):
        self.full()
        raw = (self.shared_root / AMENDMENT).read_bytes()
        self.full()
        self.full(enroll=False)
        self.assertEqual((self.shared_root / AMENDMENT).read_bytes(), raw)
        changed = dict(self.identity, source_sha256="c" * 64)
        with self.assertRaisesRegex(ValueError, "old run_id"):
            self.full(identity=changed)
        changed["run_id"] = "fresh-full-2"
        with self.assertRaisesRegex(ValueError, "explicit fresh-run"):
            self.full(identity=changed, enroll=False)
        self.full(identity=changed)
        self.assertEqual((self.shared_root / AMENDMENT).read_bytes(), raw)

    def test_only_one_strong_role_and_unchanged_transport(self):
        changes = [
            ("api_base_url", "https://example.invalid/v1"),
            ("model_strong", "other"),
            ("api_key", "different-dummy"),
            ("fast_api_key", "different-dummy"),
            ("middle_api_key", "different-dummy"),
            ("thinking_type", "disabled"),
            ("reasoning_effort", "low"),
            ("provider_request_policy", "generic"),
            ("fast_base_url", "https://example.invalid/v1"),
            ("request_budget_path", self.root / "new-http.json"),
            ("max_http_requests", 100),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                client = FakeLLMClient(self.root, full=True)
                setattr(client.cfg, field, value)
                with self.assertRaises(ValueError):
                    self.full(client=client)
        for roles in ({"trajectory_judge": "fast"}, {"extra": "strong"}, {"judge": "fast"}):
            client = FakeLLMClient(self.root, full=True)
            client.cfg.role_tiers.update(roles)
            with self.assertRaisesRegex(ValueError, "Only missing"):
                self.full(client=client)
        self.assertFalse((self.shared_root / AMENDMENT).exists())
        self.assertEqual(self.client.calls, [])

    def test_active_unknown_trace_and_unsettled_reject_before_enrollment(self):
        for filename, record in (
            ("active-pause.json", {"status": "unknown"}),
            ("pause.json", {"status": "unknown"}),
            ("trace-errors/lost.json", {"error": "synthetic"}),
            ("requests/lost.json", {"status": "submitted"}),
            ("requests/lost.json", {"status": "unknown"}),
        ):
            path = self.shared_root / filename
            atomic_json(path, record)
            with self.assertRaises(ValueError):
                self.full()
            self.assertFalse((self.shared_root / AMENDMENT).exists())
            path.unlink()

    async def test_new_unknown_keeps_both_legacy_and_full_gate(self):
        self.client.unknown = True
        shared = self.full()
        with self.assertRaises(ConnectionError):
            await shared.scoped("new", "trajectory_judge").chat(
                role="trajectory_judge", messages=[]
            )
        legacy = SharedTransport(self.old, self.shared_root, worker_limit=None)
        for view in (shared, legacy):
            with self.assertRaisesRegex(BudgetExceeded, "unknown"):
                view.check()
        with self.assertRaises(ValueError):
            self.full(enroll=False)
        self.assertEqual(len(self.client.calls), 1)

    async def test_ledger_rollback_after_new_calls_and_original_receipt_mutation(self):
        shared = self.full()
        await shared.scoped("new", "trajectory_judge").chat(role="trajectory_judge", messages=[])
        atomic_json(self.root / "http-attempts.json", {"attempts": 307})
        with self.assertRaisesRegex(ValueError, "rolled back"):
            shared.check()
        atomic_json(self.root / "http-attempts.json", {"attempts": 306})
        with self.assertRaisesRegex(ValueError, "reset"):
            shared.check()
        atomic_json(self.root / "http-attempts.json", {"attempts": 308})
        atomic_json(self.shared_root / "requests/original.json", {"status": "completed"})
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            shared.check()

    async def test_worker_counters_remain_monotonic(self):
        shared = self.full()
        await shared.scoped("fresh-worker", "worker").chat(role="worker", messages=[])
        counts_path = self.shared_root / "worker-calls.json"
        self.assertEqual(json.loads(counts_path.read_text())["fresh-worker"], 1)
        atomic_json(counts_path, {"historic-worker": 8, "fresh-worker": 0})
        with self.assertRaisesRegex(ValueError, "worker counter was rolled back"):
            shared.check()
        atomic_json(counts_path, {"historic-worker": 7, "fresh-worker": 1})
        with self.assertRaisesRegex(ValueError, "historical worker counter was reset"):
            shared.check()

    def test_amendment_and_original_policy_tamper_rejected(self):
        shared = self.full()
        audit = json.loads((self.shared_root / AMENDMENT).read_text())
        audit["added_roles"]["other"] = "strong"
        atomic_json(self.shared_root / AMENDMENT, audit)
        with self.assertRaisesRegex(ValueError, "amendment changed"):
            shared.check()
        (self.shared_root / AMENDMENT).unlink()
        shared = self.full()
        (self.shared_root / "policy.json").write_bytes(self.original_bytes + b"\n")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            shared.check()
