"""Durable generic episode backend. Adapters own control flow; backend owns receipts."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path

from darwinagent.contracts import freeze, plain
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import transport_identity
from darwinagent.runtime.leases import execution_lease
from darwinagent.runtime.steps import AwaitingBudget, StepJournal, UnknownRequest

from .context import AgentContext, CheckRejected, EpisodeBudgetExceeded
from .contracts import EpisodeResult


class ExecutionBackend:
    def __init__(self, adapter, tools, profile, client=None, run_root=None):
        self.adapter, self.profile, self.client = adapter, profile, client
        self.run_root = Path(run_root) if run_root is not None else None
        specs = tuple(tools)
        self.tools = {spec.name: spec for spec in specs}
        if len(specs) != len(self.tools) or set(profile.capabilities) != set(self.tools):
            raise ValueError("Profile must expose exactly the registered primitives")

    def _adapter_configuration(self):
        hook = getattr(self.adapter, "identity", None)
        try:
            if hook is not None:
                if not callable(hook):
                    raise ValueError("Adapter identity must be callable")
                configuration = hook()
            elif is_dataclass(self.adapter):
                configuration = asdict(self.adapter)
            else:
                raise ValueError("Ordinary adapters must declare identity() -> JSON object")
            if inspect.isawaitable(configuration):
                if inspect.iscoroutine(configuration):
                    configuration.close()
                raise ValueError("Adapter identity must be synchronous")
            if not isinstance(configuration, Mapping):
                raise ValueError("Adapter identity must be a JSON object")
            return plain(freeze(configuration))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Adapter identity must explicitly freeze its stable configuration as a JSON object"
            ) from exc

    def identity(self):
        adapter_type = type(self.adapter)
        try:
            source = inspect.getsource(adapter_type)
        except (OSError, TypeError):
            source = adapter_type.__module__ + "." + adapter_type.__qualname__
        handlers = []
        for spec in self.tools.values():
            try:
                code = inspect.getsource(spec.handler)
            except (OSError, TypeError):
                code = spec.handler.__module__ + "." + spec.handler.__qualname__
            handlers.append({"tool": spec.declaration(), "source_id": digest(code)})
        return {
            "kind": "agent-v4",
            "receipt_store": str(self.run_root.resolve()) if self.run_root is not None else None,
            "profile_id": self.profile.fingerprint,
            "adapter_id": digest(
                {
                    "source": source,
                    "config": self._adapter_configuration(),
                }
            ),
            "tools_id": digest(handlers),
            "runtime_id": digest(
                {
                    str(p.relative_to(Path(__file__).parent.parent)): digest(p.read_text())
                    for p in sorted(Path(__file__).parent.parent.rglob("*.py"))
                }
            ),
            "transport": transport_identity(self.client),
        }

    def admit(self, kernel):
        if kernel is None:
            return {"accepted": True, "kernel_id": None, "profile_id": self.profile.fingerprint}
        kernel.verify()
        if getattr(kernel, "profile_declaration", None) != self.profile.declaration():
            raise ValueError("Kernel profile differs from fixed host profile")
        collisions = set(self.tools) & {a.id for a in kernel.assets.assets if a.kind == "F"}
        if collisions:
            raise ValueError("Macros cannot shadow primitives")
        return {
            "accepted": True,
            "kernel_id": kernel.content_id,
            "profile_id": self.profile.fingerprint,
        }

    async def run(self, task, kernel=None, *, run_id):
        if self.run_root is None or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", run_id):
            raise ValueError("An explicit safe durable run_id and run_root are required")
        admission = self.admit(kernel)
        root = self.run_root / run_id
        identity = {
            "task": task.to_dict(),
            "backend": self.identity(),
            "kernel_id": admission["kernel_id"],
        }
        with execution_lease(root / "executor.lock"):
            path = root / "identity.json"
            if path.exists():
                if json.loads(path.read_text()) != identity:
                    raise ValueError("Run identity changed; choose a new run_id")
            else:
                atomic_json(path, identity)
            result_path = root / "result.json"
            if result_path.exists():
                saved = json.loads(result_path.read_text())
                if digest(saved["result"]) != saved["digest"]:
                    raise ValueError("Saved result changed")
                return EpisodeResult(**saved["result"])
            context = AgentContext(
                task, self.profile, self.tools, self.client, StepJournal(root / "steps"), kernel
            )
            try:
                result = await self.adapter.run(task, context)
                if not isinstance(result, EpisodeResult):
                    raise TypeError("AgentAdapter.run must return EpisodeResult")
                uncertainty = self._unsettled_result(context, result)
                if uncertainty is not None:
                    return uncertainty
                context.check(
                    "final_delivery",
                    {
                        "status": result.status,
                        "final_response": result.final_response,
                        "artifacts": result.to_dict()["artifacts"],
                    },
                )
                result = context.result(
                    result.final_response, status=result.status, artifacts=result.artifacts
                )
            except CheckRejected:
                result = context.result(status="rejected")
            except UnknownRequest:
                # Not final: an externally recovered receipt can be reused on the next resume.
                return context.result(status="unknown")
            except (EpisodeBudgetExceeded, AwaitingBudget):
                result = context.result(status="budget_exhausted")
            except Exception as exc:
                if context.usage()["unknown_calls"]:
                    return context.result(status="unknown")
                context.restore_events()
                context._event("failure", error_type=type(exc).__name__)
                result = context.result(status="failed")
            uncertainty = self._unsettled_result(context, result)
            if uncertainty is not None:
                return uncertainty
            atomic_json(
                result_path, {"result": result.to_dict(), "digest": digest(result.to_dict())}
            )
            return result

    @staticmethod
    def _unsettled_result(context, result):
        usage = context.usage()
        if result.status == "unknown" or usage["unknown_calls"]:
            return context.result(status="unknown")
        if usage["pending_calls"]:
            # Locally prepared/awaiting-budget calls have not dispatched, but their
            # unresolved journal positions cannot justify a final delivery either.
            return context.result(status="budget_exhausted")
        return None
