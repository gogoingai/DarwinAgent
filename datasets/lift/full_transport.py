"""Audited, additive trajectory role on the unchanged MiniMax shared budget.

The original declaration and request receipts remain immutable. This adapter
projects only the new role during legacy declaration validation; it never edits
client configuration or creates another HTTP ledger.
"""

import hashlib
import json
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.leases import execution_lease

from .transport import SharedTransport

ROLE = "trajectory_judge"
AMENDMENT = "trajectory-role-amendment.json"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _experiment_identity(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("run_id"), str)
        or not value["run_id"]
    ):
        raise ValueError("Trajectory enrollment requires a new run/source/config identity")
    for key in ("source_sha256", "config_sha256"):
        checksum = value.get(key)
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise ValueError("Trajectory enrollment requires source_sha256 and config_sha256")
        try:
            int(checksum, 16)
        except ValueError as exc:
            raise ValueError("Invalid experiment SHA256") from exc
    # Copy through JSON: later caller mutation cannot alter the enrollment identity.
    return json.loads(json.dumps(value))


class FullSharedTransport(SharedTransport):
    """Use the existing root with an explicitly audited trajectory_judge role.

    ``enroll_role=True`` creates the additive audit and enrollment for a fresh
    experiment. Reopening an enrolled identity needs no opt-in. Changed source
    or config requires another run_id and an explicit new enrollment.
    """

    def __init__(
        self, client, root, worker_limit=8, *, enroll_role=False, experiment_identity=None
    ):
        from .scheduling import campaign_lease

        with campaign_lease(root):
            self._initialize(
                client,
                root,
                worker_limit,
                enroll_role=enroll_role,
                experiment_identity=experiment_identity,
            )

    def _initialize(self, client, root, worker_limit, *, enroll_role, experiment_identity):
        self.role_amendment = None
        self.experiment_identity = _experiment_identity(experiment_identity)
        policy = Path(root) / "policy.json"
        if not policy.is_file():
            raise ValueError("Trajectory role requires the existing original shared policy")
        original = json.loads(policy.read_text())
        roles = dict(original["transport"]["role_tiers"])
        if ROLE in roles or client.cfg.role_tiers != dict(roles, **{ROLE: "strong"}):
            raise ValueError("Only missing trajectory_judge=strong may be added")
        self._original_roles = roles
        self._validating_original = True
        super().__init__(client, root, worker_limit)
        self._validating_original = False
        self._assert_fixed_policy()
        self._assert_gate_settled()
        with execution_lease(self.root / "trajectory-role-amendment.lock"):
            target = self.root / AMENDMENT
            if target.exists():
                audit = json.loads(target.read_text())
                self._verify_role_amendment(audit)
            else:
                if not enroll_role:
                    raise ValueError("Explicit trajectory role enrollment is required")
                self._assert_new_run()
                audit = self._new_role_amendment()
                atomic_json(target, audit)
            self.role_amendment = audit
            enrollment = (
                self.root
                / "trajectory-role-enrollments"
                / (digest(self.experiment_identity) + ".json")
            )
            record = {
                "experiment_identity": self.experiment_identity,
                "role_amendment_sha256": digest(audit),
            }
            if enrollment.exists():
                if json.loads(enrollment.read_text()) != record:
                    raise ValueError("Trajectory experiment enrollment changed")
            else:
                if not enroll_role:
                    raise ValueError("New source/config requires explicit fresh-run enrollment")
                self._assert_new_run()
                for previous in enrollment.parent.glob("*.json"):
                    prior = json.loads(previous.read_text())["experiment_identity"]
                    if prior["run_id"] == self.experiment_identity["run_id"]:
                        raise ValueError("Changed source/config cannot resume the old run_id")
                atomic_json(enrollment, record)
            self._enrollment_path = enrollment
            self._enrollment_sha256 = _sha(enrollment)
        self.generation_max_tokens = 16384

    def scoped(self, session, role):
        from .rate_control import RateLimitedSessionTransport

        return RateLimitedSessionTransport(self, session, role)

    def identity(self):
        result = super().identity()
        if self._validating_original:
            result["transport"] = dict(result["transport"], role_tiers=self._original_roles)
        elif self.role_amendment:
            result["trajectory_role_amendment_sha256"] = digest(self.role_amendment)
            result["trajectory_experiment_identity"] = self.experiment_identity
        return result

    def _assert_new_run(self):
        for receipt in (self.root / "requests").glob("*.json"):
            if json.loads(receipt.read_text()).get("run_id") == self.experiment_identity["run_id"]:
                raise ValueError("Trajectory source/config requires a fresh run_id")

    def _assert_fixed_policy(self):
        cfg = self.cfg
        if (
            cfg.model_strong != "MiniMax-M3.1-Flash-Preview"
            or cfg.model_fast != cfg.model_strong
            or cfg.model_middle != cfg.model_strong
            or cfg.api_base_url != "https://api.minimax.cn/v1"
            or cfg.provider_request_policy != "minimax-adaptive-v1"
            or cfg.thinking_type != "adaptive"
            or cfg.reasoning_effort != "max"
            or cfg.max_http_requests is not None
            or self.worker_limit is not None
            or cfg.middle_api_key != ""
            or cfg.request_budget_path is None
        ):
            raise ValueError("Trajectory role requires the fixed unbounded MiniMax policy")
        if (
            Path(cfg.request_budget_path).resolve()
            != (self.root.parent / "http-attempts.json").resolve()
        ):
            raise ValueError("Trajectory role must reuse the existing MiniMax HTTP ledger")

    def _assert_gate_settled(self):
        from darwinagent.runtime.model_disposition import ModelDispositionStore

        dispositions = ModelDispositionStore(self.root)
        active = self.root / "active-pause.json"
        if (active.exists() and not dispositions.pause_acknowledged(active)) or list(
            (self.root / "trace-errors").glob("*.json")
        ):
            raise ValueError("Unresolved active/trace pause forbids trajectory enrollment")
        pause = self.root / "pause.json"
        recovery = self.root / "recovery/local-cap.json"
        if pause.exists() and (
            (
                not recovery.exists()
                or json.loads(recovery.read_text()).get("pause_sha256") != _sha(pause)
            )
            and not self.settled_inflight_acknowledged(pause)
        ):
            raise ValueError("Unresolved pause forbids trajectory enrollment")
        records = []
        for path in (self.root / "requests").glob("*.json"):
            row = json.loads(path.read_text())
            if row.get("status") not in {"completed", "failed", "budget_exhausted"} and not (
                row.get("status") == "unknown" and dispositions.acknowledged(path)
            ):
                raise ValueError("Unsettled/unknown requests forbid trajectory enrollment")
            records.append(row)
        if sum(r.get("actual_http_attempts", 0) for r in records) != self.client.http_attempts():
            raise ValueError("Existing HTTP ledger and settled receipts do not reconcile")

    def _new_role_amendment(self):
        counter = self.root / "worker-calls.json"
        immutable = [self.root / "policy.json"]
        immutable += [
            self.root / name
            for name in (
                "http-cap-amendment.json",
                "worker-limit-amendment.json",
                "pause.json",
                "recovery/local-cap.json",
            )
            if (self.root / name).exists()
        ]
        audit = {
            "schema": 1,
            "authorization": "explicit_full_lift_trajectory_role",
            "added_roles": {ROLE: "strong"},
            "http_attempts": self.client.http_attempts(),
            "http_ledger": str(Path(self.cfg.request_budget_path).resolve()),
            "transport": super().identity(),
            "initial_experiment_identity": self.experiment_identity,
            "immutable_files": {str(p.relative_to(self.root)): _sha(p) for p in immutable},
            "http_ledger_snapshot_sha256": _sha(self.cfg.request_budget_path),
            "worker_counter_snapshot_sha256": _sha(counter) if counter.exists() else None,
            "historical_worker_counts": json.loads(counter.read_text()) if counter.exists() else {},
            "settled_receipts": {
                p.name: _sha(p) for p in sorted((self.root / "requests").glob("*.json"))
            },
        }
        self._verify_role_amendment(audit)
        return audit

    def _verify_role_amendment(self, audit):
        if audit.get("schema") != 1 or audit.get("added_roles") != {ROLE: "strong"}:
            raise ValueError("Invalid trajectory role amendment")
        if audit.get("transport") != super().identity():
            raise ValueError("Amended trajectory transport policy changed")
        if audit.get("http_ledger") != str(Path(self.cfg.request_budget_path).resolve()):
            raise ValueError("Amended trajectory HTTP ledger changed")
        allowed = {
            "policy.json",
            "http-cap-amendment.json",
            "worker-limit-amendment.json",
            "pause.json",
            "recovery/local-cap.json",
        }
        if "policy.json" not in audit["immutable_files"]:
            raise ValueError("Trajectory audit must bind the original policy")
        for name, checksum in audit["immutable_files"].items():
            if name not in allowed or _sha(self.root / name) != checksum:
                raise ValueError("Original trajectory policy/pause evidence changed")
        if self.client.http_attempts() < audit["http_attempts"]:
            raise ValueError("Trajectory HTTP ledger was reset")
        counts_path = self.root / "worker-calls.json"
        counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
        if any(
            counts.get(sid, 0) < used for sid, used in audit["historical_worker_counts"].items()
        ):
            raise ValueError("Trajectory historical worker counter was reset")
        for name, checksum in audit["settled_receipts"].items():
            if Path(name).name != name or _sha(self.root / "requests" / name) != checksum:
                raise ValueError("Original settled trajectory request receipt changed")
        records = [json.loads(p.read_text()) for p in (self.root / "requests").glob("*.json")]
        if sum(r.get("actual_http_attempts", 0) for r in records) > self.client.http_attempts():
            raise ValueError("Trajectory HTTP ledger was rolled back after new requests")
        per_session = {}
        for record in records:
            if record.get("role") == "worker":
                sid = record["session"]
                per_session[sid] = per_session.get(sid, 0) + 1
        if any(counts.get(sid, 0) < used for sid, used in per_session.items()):
            raise ValueError("Trajectory cumulative worker counter was rolled back")

    def check(self):
        with self.campaign_lease():
            return self._check_full_gate()

    def _check_full_gate(self):
        # Run the same legacy gate first: new unknown receipts quarantine both views.
        super().check()
        self._assert_fixed_policy()
        if self.generation_max_tokens != 16384:
            raise ValueError("Trajectory generation policy must remain16384")
        if json.loads((self.root / AMENDMENT).read_text()) != self.role_amendment:
            raise ValueError("Trajectory amendment changed")
        if _sha(self._enrollment_path) != self._enrollment_sha256:
            raise ValueError("Trajectory experiment enrollment changed")
        self._verify_role_amendment(self.role_amendment)
