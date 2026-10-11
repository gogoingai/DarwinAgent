"""Per-episode context mediates all model/leaf execution and lifecycle checks."""

from __future__ import annotations

import asyncio
import inspect
import json
from string import Template

import yaml

from darwinagent.contracts import freeze, plain
from darwinagent.kernel.spec import validate_value
from darwinagent.operators.sandbox import Interpreter, Limits, SandboxError, admit
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.journal_client import JournalClient
from darwinagent.runtime.steps import UnknownRequest

from .contracts import EpisodeResult
from .failures import ToolFailure
from .graph import project
from .interpreter import AsyncInterpreter


class CheckRejected(ValueError):
    agent_signal = True


class EpisodeBudgetExceeded(RuntimeError):
    agent_signal = True


class AgentContext:
    def __init__(self, task, profile, tools, client, journal, kernel=None, limits=Limits()):
        self.task, self.profile, self.tools = task, profile, tools
        self.journal, self.kernel, self.limits = journal, kernel, limits
        self.client = JournalClient(client, journal) if client is not None else None
        self.events = []
        self.functions, self.checks = {}, []
        self.schema = None
        if kernel is not None:
            kernel.verify()
            for a in kernel.assets.assets:
                if a.kind == "F":
                    self.functions[a.id] = (a, admit(a.content, capability_names={"call", "view"}))
                elif a.kind == "C":
                    self.checks.append((a, admit(a.content, "C")))
                elif a.kind == "S":
                    self.schema = yaml.safe_load(a.content)
        self._macro_depth = 0

    def _event(self, kind, **data):
        event = {"index": len(self.events), "kind": kind, **plain(data)}
        path = self.journal.root.parent / "events" / f"{len(self.events):06d}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            if saved != {"event": event, "digest": digest(event)}:
                raise ValueError("Immutable execution event changed on replay")
        else:
            atomic_json(path, {"event": event, "digest": digest(event)})
        self.events.append(freeze(event))

    def restore_events(self):
        events = []
        for path in sorted((self.journal.root.parent / "events").glob("[0-9]*.json")):
            row = json.loads(path.read_text())
            if digest(row["event"]) != row["digest"] or row["event"]["index"] != len(events):
                raise ValueError("Immutable execution event changed")
            events.append(freeze(row["event"]))
        self.events = events

    def _next_saved(self, kind, payload):
        path = self.journal.root / f"{self.journal.position:06d}.json"
        if path.exists():
            row = json.loads(path.read_text())
            semantic = (
                payload
                if kind != "model"
                else {k: v for k, v in payload.items() if k not in {"temperature", "max_tokens"}}
            )
            if (
                row["fingerprint"] != digest({"kind": kind, "input": semantic})
                or row["input"] != payload
            ):
                raise ValueError("Agent step input changed; use a new run_id")
            return True
        return False

    def _budget(self, kind, payload, limit):
        if self._next_saved(kind, payload):
            return
        used = sum(
            json.loads(p.read_text())["kind"] == kind for p in self.journal.root.glob("[0-9]*.json")
        )
        if limit is not None and used >= limit:
            raise EpisodeBudgetExceeded(f"{kind} budget exhausted: {used}/{limit}")

    def tool_descriptions(self):
        return [spec.declaration() for spec in self.tools.values()] + [
            {
                "name": a.id,
                "description": a.description,
                "input_contract": plain(a.input_contract),
                "output_contract": plain(a.output_contract),
                "effect": "macro",
            }
            for a, _ in self.functions.values()
        ]

    def view(self):
        if self.kernel is not None:
            self.kernel.verify()
        if self.schema is None:
            return {
                "task": self.task.to_dict(),
                "events": plain(self.events),
                "nodes": [],
                "relations": [],
            }
        return project(self.schema, self.task, self.events)

    def check(self, stage, candidate):
        if stage not in self.profile.check_stages:
            raise ValueError("Unregistered check stage")
        if self.kernel is not None:
            self.kernel.verify()
        frozen = freeze(candidate)
        outcomes = []
        for check in self.profile.host_checks:
            result = check(stage, frozen)
            if inspect.isawaitable(result):
                raise ValueError("Host checks must be synchronous and side-effect-free")
            outcomes.append(
                {
                    "id": f"fixed.{check.__module__}."
                    + getattr(check, "__qualname__", type(check).__qualname__),
                    "accepted": result is True,
                }
            )
        for a, fn in self.checks:
            if a.stage == stage:
                validate_value(frozen, a.input_contract, "check.candidate")
                result = Interpreter(fn, {}, self.limits).execute(frozen)
                validate_value(result, a.output_contract, "check.result")
                outcomes.append({"id": a.id, "accepted": result is True})
        self._event(
            "check", stage=stage, outcomes=outcomes, candidate_digest=digest(plain(candidate))
        )
        if any(not row["accepted"] for row in outcomes):
            raise CheckRejected(f"{stage} rejected")
        return True

    def prompt(self, role="worker"):
        if role not in self.profile.prompt_roles:
            raise ValueError("Unregistered prompt role")
        slots = {
            "instruction": self.task.instruction,
            "tools": json.dumps(self.tool_descriptions(), ensure_ascii=False),
            "view": json.dumps(self.view(), ensure_ascii=False),
        }
        if self.kernel is None:
            return self.task.instruction
        self.kernel.verify()
        asset = next(a for a in self.kernel.assets.assets if a.kind == "P" and a.role == role)
        return Template(asset.content).substitute(
            {k: slots[k] for k in self.profile.prompt_roles[role] if k in slots}
        )

    async def model(
        self, messages, *, role="worker", temperature=0, max_tokens=4096, json_mode=True
    ):
        if role not in self.profile.prompt_roles or self.client is None:
            raise ValueError("Unregistered model role or no client")
        payload = {
            "role": role,
            "messages": plain(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
            "namespace": "agent_worker",
            "use_cache": False,
        }
        self._budget("model", payload, self.profile.max_model_calls)
        result = await self.client.chat(**payload)
        self._event(
            "model",
            role=role,
            response_digest=digest(result.content),
            usage=getattr(result, "usage", {}),
        )
        return result

    async def call(self, name, params):
        if self.kernel is not None:
            self.kernel.verify()
        if name in self.functions:
            if self._macro_depth:
                raise SandboxError("Nested macros are forbidden")
            a, fn = self.functions[name]
            validate_value(params, a.input_contract)
            self.check("before_action", {"name": name, "params": plain(params), "effect": "macro"})
            self._macro_depth += 1
            try:
                try:
                    async with asyncio.timeout(self.limits.timeout_s):
                        result = await AsyncInterpreter(
                            fn, {"call": self._leaf_call, "view": self.view}, self.limits
                        ).execute(params)
                except TimeoutError:
                    if self.usage()["unknown_calls"]:
                        raise UnknownRequest("Macro timeout left an unreceipted call") from None
                    raise EpisodeBudgetExceeded("Macro time budget exhausted") from None
                validate_value(result, a.output_contract)
            finally:
                self._macro_depth -= 1
            self._event("macro", name=name, params=plain(params), output=plain(result))
            self.check(
                "after_action",
                {"name": name, "params": plain(params), "output": result, "effect": "macro"},
            )
            return result
        return await self._leaf_call(name, params)

    async def _leaf_call(self, name, params):
        if name not in self.tools or name not in self.profile.capabilities:
            raise ValueError("Unregistered tool capability")
        spec = self.tools[name]
        validate_value(params, spec.input_contract)
        candidate = {"name": name, "params": plain(params), "effect": spec.effect}
        self.check("before_action", candidate)
        payload = {"name": name, "params": plain(params), "effect": spec.effect}
        self._budget("tool", payload, self.profile.max_leaf_calls)
        self._event(
            "tool_call",
            name=name,
            params=plain(params),
            effect=spec.effect,
            receipt=f"{self.journal.position:06d}.json",
        )
        path, saved = self.journal.reserve("tool", payload)
        replayed = saved is not None
        if not replayed:
            # All submissions, including reads, are quarantined after uncertain completion.
            # No assumption that a nominal read-only callback is safe to execute twice.
            self.journal.submitted(path)
            try:
                result = spec.handler(freeze(params))
                if inspect.isawaitable(result):
                    result = await result
                result = plain(result)
                validate_value(result, spec.output_contract)
                self.journal.respond(path, {"output": result})
                saved = {"output": result}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A write may have completed before its callback raised. Retain submitted state.
                if spec.effect == "write":
                    raise UnknownRequest(
                        f"Write has no receipt at {path}: {type(exc).__name__}"
                    ) from None
                failure = ToolFailure.from_exception(name, exc)
                saved = {"failure": failure.to_dict()}
                self.journal.respond(path, saved)
        if "failure" in saved:
            failure = ToolFailure(**saved["failure"])
            if failure.tool_name != name:
                raise ValueError("Tool failure receipt belongs to another primitive")
            self._event("tool_failure", name=name, failure=failure.to_dict(), receipt=path.name)
            self.check("after_action", {**candidate, "failure": failure.to_dict()})
            raise failure
        result = saved["output"]
        self._event(
            "tool",
            name=name,
            params=plain(params),
            effect=spec.effect,
            output=result,
            receipt=path.name,
        )
        self.check("after_action", {**candidate, "output": result})
        return result

    def usage(self):
        rows = [json.loads(p.read_text()) for p in sorted(self.journal.root.glob("[0-9]*.json"))]
        models = [r for r in rows if r["kind"] == "model"]
        tools = [r for r in rows if r["kind"] == "tool"]
        return {
            "model_calls": len(models),
            "leaf_calls": len(tools),
            "unknown_calls": sum(r["state"] == "submitted" for r in rows),
            "pending_calls": sum(r["state"] in {"prepared", "submitted"} for r in rows),
            "failed_leaf_calls": sum("failure" in r.get("response", {}) for r in tools),
            "prompt_tokens": sum(
                r.get("response", {}).get("usage", {}).get("prompt_tokens", 0) for r in models
            ),
            "completion_tokens": sum(
                r.get("response", {}).get("usage", {}).get("completion_tokens", 0) for r in models
            ),
        }

    def result(self, final_response="", *, status="completed", artifacts=()):
        return EpisodeResult(
            status, final_response, tuple(artifacts), tuple(self.events), self.usage()
        )
