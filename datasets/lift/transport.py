"""One host transport, persistent pause gate, and cumulative per-session worker caps."""

import hashlib
import json
import time
import uuid
from pathlib import Path

from openai import APIStatusError

from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.budgets import counter_transaction
from darwinagent.runtime.identity import transport_identity
from darwinagent.runtime.leases import execution_lease


class SharedTransport:
    def __init__(self, client, root, worker_limit=8):
        self.client, self.root, self.worker_limit = client, Path(root), worker_limit
        self.cfg = client.cfg
        self.trace = False
        self.run_id = self.root.name
        self.root.mkdir(parents=True, exist_ok=True)
        self.cap_amendment = None
        policy = self.root / "policy.json"
        declaration = self.identity()
        amendment = self.root / "http-cap-amendment.json"
        if amendment.exists():
            self.cap_amendment = self._verify_cap_amendment(json.loads(amendment.read_text()))
            if self.cfg.max_http_requests != self.cap_amendment["to_cap"]:
                raise ValueError("Use the explicitly amended total HTTP cap")
            declaration = self.identity()
        if policy.exists():
            original = json.loads(policy.read_text())
            comparable = dict(declaration)
            comparable.pop("http_cap_amendment_sha256", None)
            if self.cap_amendment:
                comparable["http_cap"] = self.cap_amendment["from_cap"]
            if original != comparable:
                raise ValueError("Shared budget transport policy changed")
        else:
            if self.cfg.max_http_requests is not None and self.cfg.max_http_requests > 100:
                raise ValueError("An explicit audited HTTP cap amendment is required")
            atomic_json(policy, declaration)

    def identity(self):
        identity = {
            "transport": transport_identity(self.client),
            "worker_limit": self.worker_limit,
            "budget_root": str(self.root.resolve()),
            "http_cap": self.cfg.max_http_requests,
            "http_ledger": str(Path(self.cfg.request_budget_path).resolve())
            if self.cfg.request_budget_path
            else None,
        }
        if self.cap_amendment:
            from darwinagent.runtime.artifacts import digest

            identity["http_cap_amendment_sha256"] = digest(self.cap_amendment)
        return identity

    def _verify_cap_amendment(self, audit):
        policy = self.root / "policy.json"
        if (audit.get("from_cap"), audit.get("to_cap")) != (100, 500):
            raise ValueError("Only the explicit 100-to-500 total amendment is supported")
        if audit["original_policy_sha256"] != hashlib.sha256(policy.read_bytes()).hexdigest():
            raise ValueError("Original HTTP policy changed")
        original = json.loads(policy.read_text())
        if original["http_cap"] != 100 or audit["http_ledger"] != original["http_ledger"]:
            raise ValueError("Amendment does not bind the original HTTP ledger")
        if self.client.http_attempts() < audit["http_attempts"]:
            raise ValueError("Amended HTTP ledger was reset")
        pause_hash = audit.get("original_pause_sha256")
        if (
            pause_hash
            and hashlib.sha256((self.root / "pause.json").read_bytes()).hexdigest() != pause_hash
        ):
            raise ValueError("Original pause evidence changed")
        for name, checksum in audit["settled_receipts"].items():
            path = self.root / "requests" / name
            if Path(name).name != name or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError("Original settled request receipt changed")
        return audit

    def amend_http_cap(self, *, total_cap, expected_http_attempts):
        """Explicit coordinator audit; preserve all original bytes and counters.

        This API writes only an immutable authorization record. Recreate the
        client with cap=500 afterwards; ordinary startup never raises a cap.
        """
        if total_cap != 500 or self.cfg.max_http_requests != 100:
            raise ValueError("Amendment requires the original 100 cap and authorized total 500")
        with execution_lease(self.root / "http-cap-amendment.lock"):
            target = self.root / "http-cap-amendment.json"
            if target.exists():
                audit = self._verify_cap_amendment(json.loads(target.read_text()))
                if audit["http_attempts"] != expected_http_attempts:
                    raise ValueError("Amendment expected-attempt evidence changed")
                return audit
            actual = self.client.http_attempts()
            if actual != expected_http_attempts or not 0 <= actual < 100:
                raise ValueError("HTTP attempts changed or the original global cap is exhausted")
            pause = self.root / "pause.json"
            if (self.root / "active-pause.json").exists():
                raise ValueError("Unresolved active pause forbids amendment")
            if pause.exists():
                recovered = self.root / "recovery/local-cap.json"
                if (
                    not recovered.exists()
                    or json.loads(recovered.read_text())["pause_sha256"]
                    != hashlib.sha256(pause.read_bytes()).hexdigest()
                ):
                    raise ValueError("Unresolved unknown/trace/global pause forbids amendment")
            receipts = {
                p.name: (p, json.loads(p.read_text()))
                for p in (self.root / "requests").glob("*.json")
            }
            settled = {"completed", "failed", "budget_exhausted"}
            if not receipts or any(r["status"] not in settled for _, r in receipts.values()):
                raise ValueError("Every original HTTP request must have a settled receipt")
            if sum(r.get("actual_http_attempts", 0) for _, r in receipts.values()) != actual:
                raise ValueError("Original settled HTTP receipts do not reconcile")
            if list((self.root / "trace-errors").glob("*.json")):
                raise ValueError("Unresolved trace failure forbids amendment")
            policy = self.root / "policy.json"
            original = json.loads(policy.read_text())
            if original != self.identity() or original["http_cap"] != 100:
                raise ValueError("Original transport policy changed")
            counter = self.root / "worker-calls.json"
            audit = {
                "from_cap": 100,
                "to_cap": 500,
                "http_attempts": actual,
                "authorization": "explicit_user_total_cap",
                "http_ledger": original["http_ledger"],
                "original_policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
                "original_http_ledger_sha256": hashlib.sha256(
                    Path(original["http_ledger"]).read_bytes()
                ).hexdigest()
                if original["http_ledger"]
                else None,
                "original_worker_counter_sha256": hashlib.sha256(counter.read_bytes()).hexdigest()
                if counter.exists()
                else None,
                "original_pause_sha256": hashlib.sha256(pause.read_bytes()).hexdigest()
                if pause.exists()
                else None,
                "settled_receipts": {
                    name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, (path, _) in sorted(receipts.items())
                },
            }
            atomic_json(target, audit)
            return audit

    def check(self):
        if (self.root / "http-cap-amendment.json").exists() and self.cfg.max_http_requests != 500:
            raise BudgetExceeded("Recreate transport with the explicitly amended total HTTP cap")
        active = self.root / "active-pause.json"
        path = active if active.exists() else self.root / "pause.json"
        for request in (self.root / "requests").glob("*.json"):
            if json.loads(request.read_text())["status"] in {"submitted", "unknown"}:
                self.pause("unknown")
                raise BudgetExceeded("Native scheduling paused: unknown", dispatched=True)
        recovered = self.root / "recovery" / "local-cap.json"
        acknowledged = (
            recovered.exists()
            and path.name == "pause.json"
            and path.exists()
            and json.loads(recovered.read_text())["pause_sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )
        if path.exists() and not acknowledged:
            raise BudgetExceeded(
                "Native scheduling paused: " + json.loads(path.read_text())["status"]
            )
        if (
            self.cfg.max_http_requests is not None
            and self.client.http_attempts() >= self.cfg.max_http_requests
        ):
            self.pause("budget_exhausted")
            raise BudgetExceeded("Shared HTTP attempt budget exhausted")

    def pause(self, status):
        # The amendment permanently binds historical pause bytes. Later gates
        # live separately and cannot be acknowledged by that old recovery.
        historical = self.cap_amendment and self.cap_amendment.get("original_pause_sha256")
        path = self.root / ("active-pause.json" if historical else "pause.json")
        if not path.exists() or (
            not historical and (self.root / "recovery" / "local-cap.json").exists()
        ):
            atomic_json(path, {"status": status, "gate_id": uuid.uuid4().hex})

    def recover_local_cap_pause(self, *, session_root, expected_http_attempts):
        """Explicit offline audit; preserve old pause bytes and every budget counter.

        Must be called with the stopped historical run's actual worker session.
        A new immutable audit acknowledges exactly the proven legacy pause bytes.
        It cannot acknowledge any newer/global/trace/unknown gate.
        """
        session_root = Path(session_root).resolve()
        with execution_lease(self.root / "recovery.lock"):
            pause = self.root / "pause.json"
            raw = pause.read_bytes()
            if json.loads(raw) != {"status": "budget_exhausted"}:
                raise ValueError("Only the legacy ambiguous local-cap pause can be audited")
            actual = self.client.http_attempts()
            if actual != expected_http_attempts or not 0 <= actual < self.cfg.max_http_requests:
                raise ValueError("HTTP budget changed or globally exhausted")
            requests = [json.loads(p.read_text()) for p in (self.root / "requests").glob("*.json")]
            if not requests or any(r["status"] != "completed" for r in requests):
                raise ValueError("Every historical request must have a settled completed receipt")
            if sum(r["actual_http_attempts"] for r in requests) != actual:
                raise ValueError("Historical HTTP receipts do not reconcile")
            identity = json.loads((session_root / "session-identity.json").read_text())
            sid = identity["session"]
            counts = json.loads((self.root / "worker-calls.json").read_text())
            matching = [r for r in requests if r["session"] == sid]
            if (
                identity["role"] != "worker"
                or counts.get(sid) != self.worker_limit
                or len(matching) != self.worker_limit
            ):
                raise ValueError("Missing exact worker-session cap proof")
            if {r["run_id"] for r in matching} != {identity["run_id"]}:
                raise ValueError("Historical session/run identity mismatch")
            proof = []
            run_root = session_root.parent.parent
            for step in run_root.glob("sessions/*/episodes/*/steps/[0-9]*.json"):
                value = json.loads(step.read_text())
                if value["state"] == "submitted":
                    raise ValueError("Historical unknown model or write effect remains")
                if value["state"] == "prepared":
                    if (
                        not step.is_relative_to(session_root)
                        or value.get("pending_budget")
                        != "Cumulative worker session budget exhausted"
                    ):
                        raise ValueError("Unexplained pending historical step")
                    proof.append(
                        {"path": str(step), "sha256": hashlib.sha256(step.read_bytes()).hexdigest()}
                    )
            if (
                not proof
                or list(run_root.glob("sessions/*/trace-errors/*.json"))
                or list((self.root / "trace-errors").glob("*.json"))
            ):
                raise ValueError("Missing local-cap journal proof or unresolved trace failure")
            audit = {
                "pause_sha256": hashlib.sha256(raw).hexdigest(),
                "session": sid,
                "session_root": str(session_root),
                "http_attempts": actual,
                "worker_count": counts[sid],
                "proof": proof,
                "reason": "legacy_local_worker_cap",
            }
            target = self.root / "recovery" / "local-cap.json"
            if target.exists():
                if json.loads(target.read_text()) != audit:
                    raise ValueError("Recovery evidence changed")
                return audit
            archive = self.root / "recovery" / "original-pause.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            if archive.exists() and archive.read_bytes() != raw:
                raise ValueError("Original pause archive changed")
            if not archive.exists():
                archive.write_bytes(raw)
            atomic_json(target, audit)
            return audit

    def scoped(self, session, role):
        return SessionTransport(self, session, role)


class SessionTransport:
    def __init__(self, shared, session, role):
        self.shared, self.session, self.role = shared, session, role
        self.cfg = shared.cfg

    async def chat(self, **kwargs):
        self.shared.check()
        if self.role == "worker":
            with counter_transaction(self.shared.root / "worker-calls.json") as counts:
                used = counts.get(self.session, 0)
                if used >= self.shared.worker_limit:
                    raise BudgetExceeded("Cumulative worker session budget exhausted")
                counts[self.session] = used + 1
        call_id = uuid.uuid4().hex
        path = self.shared.root / "requests" / (call_id + ".json")
        record = {
            "id": call_id,
            "session": self.session,
            "run_id": self.shared.run_id,
            "role": kwargs.get("role", self.role),
            "namespace": kwargs.get("namespace"),
            "status": "submitted",
            "started": time.time(),
            "usage": None,
            "model": self.cfg.model_strong,
        }
        atomic_json(path, record)
        before_http = self.shared.client.http_attempts()
        try:
            response = await self.shared.client.chat(
                **dict(kwargs, temperature=0, durable=True, use_cache=False)
            )
        except BudgetExceeded as exc:
            record["status"] = "unknown" if exc.dispatched else "budget_exhausted"
            self.shared.pause(record["status"])
            raise
        except APIStatusError as exc:
            record.update(
                status="failed", error_type=type(exc).__name__, http_status=exc.status_code
            )
            raise
        except Exception:
            record["status"] = "unknown"
            self.shared.pause("unknown")
            raise
        else:
            record.update(
                status="completed", usage=getattr(response, "usage", {}), output=response.content
            )
            return response
        finally:
            record["finished"] = time.time()
            record["actual_http_attempts"] = self.shared.client.http_attempts() - before_http
            atomic_json(path, record)
            if self.shared.trace and self.role not in {"worker", "judge"}:
                from .tracing import emit_model_role

                try:
                    emit_model_role(self.shared, record)
                except Exception as exc:
                    atomic_json(
                        self.shared.root / "trace-errors" / (call_id + ".json"),
                        {"request": call_id, "error_type": type(exc).__name__},
                    )
                    self.shared.pause("trace_incomplete")

    async def aclose(self):
        # Campaign factories close role views; the coordinator owns the shared transport.
        pass
