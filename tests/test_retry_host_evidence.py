"""Synthetic receipt fixtures: no requests and no live acceptance claims."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.llm.client import LLMClient
from darwinagent.runtime.artifacts import digest
from datasets.lift.full_report import host_evidence, role_costs


class RetryHostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cfg = SimpleNamespace(
            api_base_url="https://api.minimax.cn/v1",
            model_strong="MiniMax-M3.1-Flash-Preview",
            provider_request_policy="minimax-adaptive-v1",
        )
        # Host type guard is exercised with an inert object, never initialized as a client.
        shared = SimpleNamespace(
            client=object.__new__(LLMClient), cfg=cfg, root=self.root / "shared"
        )
        self.owner = SimpleNamespace(root=self.root, shared=shared, verify=lambda: None)
        self.records = []
        for index, role in enumerate(("worker", "judge", "trajectory_judge")):
            output = role + "-output"
            usage = {"prompt_tokens": 10, "completion_tokens": 2}
            self.records.append(
                dict(
                    id=role,
                    role=role,
                    session=role,
                    run_id="offline",
                    namespace=role,
                    status="completed",
                    provider_request_policy=cfg.provider_request_policy,
                    model=cfg.model_strong,
                    actual_http_attempts=1,
                    started=index + 1,
                    finished=index + 1.5,
                    request_fingerprint=digest({"role": role}),
                    max_tokens=16384,
                    output=output,
                    usage=usage,
                )
            )
            if role != "trajectory_judge":
                path = self.root / "episodes/e" / role / "chat.json"
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "turns": [
                                {
                                    "episode": {
                                        "events": [
                                            {
                                                "kind": "model",
                                                "response_digest": digest(output),
                                                "usage": usage,
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    )
                )
        self.row = {
            "evidence_key": "e",
            "native": {"work_session_id": "worker", "judge_session_id": "judge"},
            "trajectory": {
                "status": "completed",
                "identity": {"session": "trajectory_judge"},
                "raw_response": "trajectory_judge-output",
            },
        }

    def retry(self, role, status=429):
        completed = next(r for r in self.records if r["role"] == role)
        failed = {k: v for k, v in completed.items() if k not in {"output", "usage"}}
        failed.update(
            id=role + "-rejected",
            status="failed",
            http_status=status,
            started=completed["started"] - 0.8,
            finished=completed["started"] - 0.3,
        )
        self.records.append(failed)
        self.sync_audit()
        return failed

    def sync_audit(self):
        from datasets.lift.rate_control import PROTOCOL

        root = self.owner.shared.root
        (root / "requests").mkdir(parents=True, exist_ok=True)
        (root / "service-rate-events").mkdir(parents=True, exist_ok=True)
        state = dict(protocol=PROTOCOL, event_count=0, consecutive_429=0, cooldown_until=0.0)
        for index, row in enumerate(sorted(self.records, key=lambda r: r["started"]), 1):
            path = root / "requests" / (row["id"] + ".json")
            path.write_text(json.dumps(row))
            next_state = dict(
                protocol=PROTOCOL,
                event_count=index,
                consecutive_429=state["consecutive_429"] + 1 if row["status"] == "failed" else 0,
                cooldown_until=100.0 if row["status"] == "failed" else 0.0,
            )
            event = dict(
                previous_state_sha256=digest(state),
                receipt=path.name,
                receipt_sha256=digest(row),
                state=next_state,
            )
            (root / "service-rate-events" / f"{index:08d}.json").write_text(
                json.dumps(dict(event, sha256=digest(event)))
            )
            state = next_state
        (root / "service-rate-control.json").write_text(
            json.dumps(dict(state, sha256=digest(state)))
        )

    def test_no_retry_baseline_matches_one_event_per_role(self):
        self.assertTrue(host_evidence(self.owner, [self.row], self.records))

    def test_completed_worker_after_known429_retains_valid_host_evidence(self):
        self.retry("worker")
        self.assertTrue(host_evidence(self.owner, [self.row], self.records))
        self.assertEqual(role_costs(self.records)["worker"]["actual_http_attempts"], 2)

    def test_completed_trajectory_after_known429_retains_valid_host_evidence(self):
        self.retry("trajectory_judge")
        self.assertTrue(host_evidence(self.owner, [self.row], self.records))

    def test_unknown_must_still_fail_host_evidence(self):
        row = self.retry("worker")
        row["status"] = "unknown"
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_non429_failure_must_still_fail_host_evidence(self):
        self.retry("worker", 500)
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_extra_completion_must_not_match_a_single_logical_event(self):
        row = copy.deepcopy(self.records[0])
        row["id"] = "extra"
        row["started"] = 20
        self.records.append(row)
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_missing_rate_event_is_not_a_verified_retry(self):
        self.retry("worker")
        (self.owner.shared.root / "service-rate-events/00000001.json").unlink()
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_retry_with_rehashed_changed_input_is_rejected(self):
        failed = self.retry("worker")
        failed["request_fingerprint"] = "f" * 64
        self.sync_audit()
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_unsettled_intent_forbids_host_verification(self):
        self.retry("worker")
        (self.owner.shared.root / "service-rate-pending.json").write_text("{}")
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_exhausted_three_rejections_cannot_be_recast_as_one_retry(self):
        for index in range(3):
            failed = self.retry("worker")
            failed["id"] = f"rejection-{index}"
            failed["started"] = 0.1 + index * 0.2
            failed["finished"] = failed["started"] + 0.1
        self.sync_audit()
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_rehashed_missing_input_identity_is_rejected(self):
        self.retry("worker")
        for row in self.records:
            if row["role"] == "worker":
                row.pop("request_fingerprint")
        self.sync_audit()
        self.assertFalse(host_evidence(self.owner, [self.row], self.records))

    def test_retry_verification_does_not_mutate_receipts_or_audits(self):
        self.retry("worker")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(host_evidence(self.owner, [self.row], self.records))
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )

    def test_unmatched_auxiliary_failures_cannot_be_hidden_by_valid_worker_retry(self):
        self.retry("worker")
        for status, http_status in (("unknown", None), ("failed", 500), ("failed", 429)):
            with self.subTest(status=status, http_status=http_status):
                extra = dict(self.records[0])
                extra.update(id="extra", session="extra", role="wiki", status=status)
                extra.pop("output")
                extra.pop("usage")
                if http_status is not None:
                    extra["http_status"] = http_status
                self.assertFalse(host_evidence(self.owner, [self.row], [*self.records, extra]))

    def test_valid_auxiliary_retry_keeps_all_costs_and_host_evidence(self):
        extra = dict(self.records[0])
        extra.update(id="wiki", session="wiki", role="wiki", started=10, finished=10.5)
        self.records.append(extra)
        self.retry("wiki")
        self.assertTrue(host_evidence(self.owner, [self.row], self.records))
        self.assertEqual(role_costs(self.records)["wiki"]["actual_http_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
