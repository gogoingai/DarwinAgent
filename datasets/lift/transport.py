"""One host transport, persistent pause gate, and cumulative per-session worker caps."""

import json
import time
import uuid
from pathlib import Path

from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.budgets import counter_transaction
from darwinagent.runtime.identity import transport_identity


class SharedTransport:
    def __init__(self, client, root, worker_limit=8):
        self.client, self.root, self.worker_limit = client, Path(root), worker_limit
        self.cfg = client.cfg
        self.trace = False
        self.run_id = self.root.name
        self.root.mkdir(parents=True, exist_ok=True)
        policy = self.root / "policy.json"
        declaration = self.identity()
        if policy.exists() and json.loads(policy.read_text()) != declaration:
            raise ValueError("Shared budget transport policy changed")
        if not policy.exists():
            atomic_json(policy, declaration)

    def identity(self):
        return {
            "transport": transport_identity(self.client),
            "worker_limit": self.worker_limit,
            "budget_root": str(self.root.resolve()),
            "http_cap": self.cfg.max_http_requests,
            "http_ledger": str(Path(self.cfg.request_budget_path).resolve())
            if self.cfg.request_budget_path
            else None,
        }

    def check(self):
        path = self.root / "pause.json"
        for request in (self.root / "requests").glob("*.json"):
            if json.loads(request.read_text())["status"] in {"submitted", "unknown"}:
                self.pause("unknown")
                break
        if path.exists():
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
        path = self.root / "pause.json"
        if not path.exists():
            atomic_json(path, {"status": status})

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
