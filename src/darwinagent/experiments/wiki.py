"""Training-only optimization memory. Facts are durable before model attribution."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from darwinagent.agents.protocol import ModelSession
from darwinagent.contracts import freeze
from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import BUILTINS, DATA_CAPABILITIES
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.budgets import counter_transaction
from darwinagent.runtime.steps import AwaitingBudget, RequestAbandoned, StepJournal, UnknownRequest

from .wiki_context import _brief_facts, _context_facts, _formal_runtime_facts
from .wiki_evidence import _compress_training_evidence, pagination_anomalies, pagination_metadata
from .wiki_evidence import asset_evidence as asset_evidence
from .wiki_evidence import bounded_trace as bounded_trace
from .wiki_evidence import safe_feedback as safe_feedback
from .wiki_evidence import safe_scores as safe_scores
from .wiki_lessons import _lessons
from .wiki_lessons import failure_patterns as failure_patterns
from .wiki_service import WikiService


class WikiMaintainer:
    def __init__(self, root, identity, client_factory, config, limit=30):
        self.root = Path(root) / "optimization"
        self.identity = identity
        self.client_factory = client_factory
        self.config = config
        self.limit = limit
        self.wiki_path = self.root / "wiki.json"
        self.state_path = self.root / "state.json"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state["identity"] != identity or state["limit"] != limit:
                state.setdefault("source_history", []).append(
                    {"identity": state["identity"], "limit": state["limit"]}
                )
                state.update(identity=identity, limit=limit)
                atomic_json(self.state_path, state)
        else:
            atomic_json(
                self.state_path, {"identity": identity, "limit": limit, "reserved_calls": 0}
            )
        if self.wiki_path.exists():
            # Historical entries retain their recorded producer identity.
            pass
        else:
            atomic_json(
                self.wiki_path,
                {"identity": identity, "version": 0, "consumed_ids": [], "entries": []},
            )

    @property
    def service(self):
        return WikiService(
            self.root.parent, lambda: self.client_factory("optimization"), self.config
        )

    def _wiki(self):
        return json.loads(self.wiki_path.read_text())

    def context(self, max_chars=18000):
        wiki = self._wiki()
        lessons = []
        for lesson in reversed(wiki.get("lessons", _lessons(wiki["entries"]))):
            if len(json.dumps([lesson, *lessons], ensure_ascii=False)) <= max_chars // 3:
                lessons.insert(0, lesson)
        selected = []
        # Latest formal/decision evidence is retained before repeated admission logs.
        ranked = sorted(
            enumerate(wiki["entries"]),
            key=lambda pair: (pair[1]["kind"] in ("formal", "decision"), pair[0]),
            reverse=True,
        )
        selected_ids = set()
        for index, entry in ranked:
            brief = {**entry, "facts": _context_facts(entry["facts"])}
            candidate = [(index, brief), *selected]
            payload = {
                "version": wiki["version"],
                "lessons": lessons,
                "entries": [e for _, e in candidate],
            }
            if len(json.dumps(payload, ensure_ascii=False)) <= max_chars:
                selected = candidate
                selected_ids.add(index)
            elif not selected:
                compact = {
                    k: entry.get(k) for k in ("id", "stage", "kind", "scope", "training_ids")
                }
                compact["facts"] = {
                    "evidence_refs": entry["facts"].get("evidence_refs", [])[:8],
                    "evidence_refs_total": len(entry["facts"].get("evidence_refs", [])),
                    "scores": entry["facts"].get("scores"),
                    "anomaly_index": entry["facts"].get("anomaly_index", [])[:4],
                    "anomaly_total": len(entry["facts"].get("anomaly_index", [])),
                    "pipeline_active_stages": entry["facts"].get("pipeline_active_stages"),
                    "asset_change_signals": [
                        {
                            **{
                                k: signal.get(k)
                                for k in ("asset_kinds", "case_id", "question_id", "confidence")
                            },
                            "observation": signal.get("observation", "")[:240],
                            "next_check": signal.get("next_check", "")[:300],
                            "evidence_view": "query evidence_refs or case scope for originals",
                        }
                        for signal in entry["facts"].get("asset_change_signals", [])[:4]
                    ],
                    "asset_change_signals_total": len(
                        entry["facts"].get("asset_change_signals", [])
                    ),
                    "view_truncated": True,
                }
                selected = [(index, compact)]
                selected_ids.add(index)
        return {
            "version": wiki["version"],
            "lessons": lessons,
            "entries": [e for _, e in sorted(selected)],
            "truncated": len(selected_ids) < len(wiki["entries"]),
        }

    def new_failure(self, facts):
        patterns = set(failure_patterns(facts))
        known = set()
        for entry in self._wiki()["entries"]:
            if entry.get("attribution") or entry.get("pending_attribution"):
                known.update(failure_patterns(entry["facts"]))
        return bool(patterns - known)

    def reconcile(self):
        for path in sorted((self.root / "events").glob("*.json")):
            event = json.loads(path.read_text())
            self._consume(event)
            self._refresh(event["id"])

    def _consume(self, event):
        wiki = self._wiki()
        if event["id"] in wiki["consumed_ids"]:
            return
        attribution_path = self.root / "maintenance" / f"{event['id']}.json"
        attribution = (
            json.loads(attribution_path.read_text()) if attribution_path.exists() else None
        )
        entry = {
            k: event[k]
            for k in ("id", "stage", "kind", "category", "scope", "training_ids", "facts", "source")
        }
        entry["producer_identity"] = event.get("producer_identity", wiki["identity"])
        entry["fact_status"] = "recorded"
        entry["confidence"] = "hypothesis"
        entry["validation_scope"] = event["scope"]
        entry["attribution"] = attribution.get("attribution") if attribution else None
        entry["pending_attribution"] = bool(event.get("infer") and not attribution)
        wiki["entries"].append(entry)
        wiki["consumed_ids"].append(event["id"])
        wiki["version"] += 1
        wiki["lessons"] = _lessons(wiki["entries"])
        atomic_json(self.wiki_path, wiki)

    async def record(
        self,
        stage,
        kind,
        facts,
        *,
        category="runtime",
        scope="trial",
        training_ids=(),
        source="",
        infer=False,
    ):
        facts = json.loads(json.dumps(facts, ensure_ascii=False))
        # Graph evidence is training-only and carries the exact saved artifacts. Keep
        # the full originals queryable while the proposer receives bounded summaries.
        previous = {}
        if kind == "formal":
            for entry in self._wiki()["entries"]:
                if entry["kind"] == "formal" and entry["stage"] != stage:
                    previous.update({g.get("case_id"): g for g in entry["facts"].get("graphs", [])})
        graph_refs = []
        for graph in facts.get("graphs", []):
            from darwinagent.kernel.revision import parse_training_id

            graph_training_ids = []
            for tid in training_ids:
                try:
                    if parse_training_id(tid)[0] == graph.get("case_id"):
                        graph_training_ids.append(tid)
                except ValueError:
                    continue
            before = previous.get(graph.get("case_id"))
            if before and "graph_digest" in graph and "graph_digest" in before:
                graph["previous_graph_digest"] = before["graph_digest"]
                graph["delta"] = {key: graph[key] - before[key] for key in ("nodes", "edges")}
            artifacts = graph.pop("artifacts", {})
            for label, filename in artifacts.items():
                path = Path(filename)
                if path.is_file():
                    data = (
                        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                        if label == "facts"
                        else json.loads(path.read_text())
                    )
                    graph_refs.append(
                        self.service.register(
                            {"case_id": graph.get("case_id"), "stage": stage, label: data},
                            scope={
                                "case_ids": [graph.get("case_id")],
                                "stage_ids": [stage],
                                "training_ids": graph_training_ids,
                            },
                        )
                    )
        originals = facts.pop("_original_training_evidence", [])
        facts.pop("evidence_refs", None)
        stable_facts = json.loads(json.dumps(facts, ensure_ascii=False))
        original_refs = []
        for original in originals:
            original_refs.append(
                self.service.register(
                    original,
                    scope={
                        "case_ids": [original["case_id"]],
                        "question_ids": [original["question_id"]],
                        "asset_ids": sorted(
                            {
                                ev["asset_id"]
                                for ev in original.get("trace", [])
                                if ev.get("asset_id")
                            }
                        ),
                    },
                )
            )
        raw_ref = self.service.register(
            facts,
            source_kind="asset" if "asset_changes" in facts else "training",
            scope={
                "asset_ids": [change.get("asset_id") for change in facts.get("asset_changes", [])],
                "stage_ids": [stage],
                "candidate_ids": [facts["candidate_version"]]
                if facts.get("candidate_version")
                else [],
            },
            source_refs=[*original_refs, *graph_refs],
        )
        facts["evidence_refs"] = [raw_ref, *original_refs, *graph_refs]
        anomaly_index = []
        for original, ref in zip(originals, original_refs):
            for position, trace_event in enumerate(original.get("trace", [])):
                hints = pagination_anomalies(trace_event)
                if hints:
                    anomaly_index.append(
                        {
                            "question_id": original["question_id"],
                            "evidence_ref": ref,
                            "trace_position": position,
                            "hints": hints,
                            "pagination": pagination_metadata(trace_event),
                            "status": "needs_contract_check",
                        }
                    )
        if anomaly_index:
            facts["anomaly_index"] = anomaly_index
        event = {
            "producer_identity": self.identity,
            "stage": stage,
            "kind": kind,
            "facts": facts,
            "category": category,
            "scope": scope,
            "training_ids": list(training_ids),
            "source": source,
            "infer": infer,
        }
        event["id"] = digest({"identity": self.identity, **event, "facts": stable_facts})
        path = self.root / "events" / f"{event['id']}.json"
        if not path.exists():
            atomic_json(path, event)
        self._consume(event)
        if infer:
            await self._attribute(event)
        if kind == "formal":
            runtime_facts = _formal_runtime_facts(facts)
            if runtime_facts:
                # Mechanical failures are separate from score/strategy hypotheses.
                # Decision attribution still sees both; this adds no model call.
                await self.record(
                    stage,
                    "runtime_faults",
                    runtime_facts,
                    category="runtime",
                    scope=scope,
                    training_ids=training_ids,
                    source=source,
                )
        return event["id"]

    def _maintenance_payload(self, event):
        from .graph_evidence import ASSET_DIAGNOSIS_PROTOCOL

        payload = {
            "asset_diagnosis_contract": ASSET_DIAGNOSIS_PROTOCOL,
            "event": {**event, "facts": _brief_facts(event["facts"])},
            "wiki_version": self._wiki()["version"],
            "recent": self.context(8000)["entries"][-4:],
            "runtime_contract": {
                "capabilities": {
                    name: str(
                        inspect.signature(getattr(DataCapabilities, name)).replace(
                            parameters=[
                                p
                                for p in inspect.signature(
                                    getattr(DataCapabilities, name)
                                ).parameters.values()
                                if p.name != "self"
                            ]
                        )
                    )
                    for name in sorted(DATA_CAPABILITIES)
                },
                "capability_implementations": {
                    name: inspect.getsource(getattr(DataCapabilities, name))
                    for name in ("nodes", "search", "traverse")
                },
                "result_shapes": {
                    "nodes": "rows[]",
                    "search": "rows[]",
                    "traverse": "rows[]",
                    "project": "rows[] (not column values)",
                    "relative_date": "object with resolved/precision",
                },
                "input_freeze_implementation": inspect.getsource(freeze),
                "builtin_names": sorted(BUILTINS),
                "asset_contract_types": {
                    "type": "single string only; union arrays are unsupported",
                    "allowed": [
                        "object",
                        "array",
                        "string",
                        "integer",
                        "number",
                        "boolean",
                        "null",
                        "any",
                    ],
                    "nullable": "normalize to declared scalar/object, or use type any; never type ['object','null']",
                },
                "candidate_boundary": "answer is text; structured_answer is the already parsed JSON value or null. C receives recursively frozen arrays (tuple) and objects (read-only mapping), so isinstance(x,list/dict) rejects valid inputs. Mandatory answer_contract checks are separate.",
                "function_steps": self.config.function_steps,
                "result_bytes": self.config.result_bytes,
            },
        }
        # Deterministic bounding preserves current evidence ahead of historical events.
        while len(json.dumps(payload, ensure_ascii=False)) > 35000 and payload["recent"]:
            payload["recent"].pop(0)
        facts = payload["event"]["facts"]
        while (
            len(json.dumps(payload, ensure_ascii=False)) > 35000
            and len(facts.get("training_examples", [])) > 1
        ):
            facts["training_examples"].pop()
            facts["training_examples_truncated"] = True
        if len(json.dumps(payload, ensure_ascii=False)) > 35000:
            for change in facts.get("asset_changes", []):
                for side in ("before", "after"):
                    asset = change.get(side)
                    if asset and len(asset.get("content", "")) > 1200:
                        asset["content"] = asset["content"][:1200]
                        asset["content_truncated"] = True
        if len(json.dumps(payload, ensure_ascii=False)) > 35000:
            # 字段级预算压缩（2026-10-05 审查 P1/二次复查 P1：超长即放弃导致 B0 formal
            # 等归因缺失）。预算按「完整 payload 减去其余部分」计——facts 压到该预算内
            # 才保证总请求 ≤35000。确定性、请求发出前：不重放模型调用，不扩大上限。
            rest = {k: v for k, v in payload.items() if k != "event"}
            rest["event"] = {k: v for k, v in payload["event"].items() if k != "facts"}
            rest_chars = len(json.dumps(rest, ensure_ascii=False, default=str))
            _compress_training_evidence(facts, budget=max(4000, 35000 - rest_chars))
        if len(json.dumps(payload, ensure_ascii=False)) > 35000:
            # Facts remain durable; an oversized attribution is a visible pending task.
            self._maintenance_failure(
                event["id"], 0, "Maintenance evidence exceeds 35000 characters"
            )
            return None
        return payload

    def _maintenance_event(self, event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
            raise ValueError("Invalid Wiki maintenance event id")
        event = json.loads((self.root / "events" / f"{event_id}.json").read_text())
        if not event.get("infer"):
            raise ValueError("Event does not request Wiki attribution")
        return event

    def _maintenance_control(self, event):
        event_id = event["id"]
        path = self.root / "maintenance" / f"{event_id}.control.json"
        if path.exists():
            return json.loads(path.read_text())
        request = self.root / "maintenance" / f"{event_id}.request.json"
        control = {"event_id": event_id, "attempt": 0, "state": "prepared", "history": []}
        if request.exists():
            # A legacy request file is not proof that dispatch failed or succeeded.
            payload = json.loads(request.read_text())
            request_id = self.service.workspace.prepare_request(
                {
                    "role": "wiki_maintainer",
                    "payload": payload,
                    "legacy_request_path": str(request),
                    "event_id": event_id,
                }
            )
            self.service.workspace.set_request_status(request_id, "unknown")
            control.update(state="unknown", legacy=True, legacy_request_id=request_id)
        atomic_json(path, control)
        return control

    def _maintenance_failure(self, event_id, attempt, error, events=()):
        value = {"error": error, "events": list(events), "attempt": attempt}
        path = self.root / "maintenance" / event_id / f"attempt-{attempt}.failure.json"
        # Append separate failure records; the first legacy failure remains unchanged.
        if path.exists():
            path = path.parent / (f"attempt-{attempt}.failure-" + digest(value) + ".json")
        atomic_json(path, value)
        legacy = self.root / "maintenance" / f"{event_id}.failure.json"
        if not legacy.exists():
            atomic_json(legacy, value)

    def retry_maintenance(self, event_id, reason):
        """Register a new attempt only; explicit resume consumes the prepared task."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Explicit reason required for Wiki maintenance retry")
        event = self._maintenance_event(event_id)
        target = self.root / "maintenance" / f"{event_id}.json"
        if target.exists():
            self._refresh(event_id)
            return {"event_id": event_id, "state": "complete"}
        control = self._maintenance_control(event)
        previous = {key: value for key, value in control.items() if key != "history"}
        control["history"].append(previous)
        control.update(
            attempt=control["attempt"] + 1,
            state="prepared",
            legacy=False,
            retry_reason=reason.strip(),
            possible_duplicate_cost=True,
        )
        atomic_json(self.root / "maintenance" / f"{event_id}.control.json", control)
        self.service.workspace.append_event(
            "wiki_maintenance_retry",
            {
                "event_id": event_id,
                "attempt": control["attempt"],
                "reason": reason.strip(),
                "possible_duplicate_cost": True,
                "previous": previous,
            },
        )
        return control

    async def resume_maintenance(self, event_id=None):
        """Explicitly resume pending attribution only, independently of completed outboxes."""
        events = (
            [self._maintenance_event(event_id)]
            if event_id is not None
            else [
                json.loads(path.read_text())
                for path in sorted((self.root / "events").glob("*.json"))
            ]
        )
        results = []
        for event in events:
            if not event.get("infer"):
                continue
            self._consume(event)
            target = self.root / "maintenance" / f"{event['id']}.json"
            if not target.exists():
                await self._attribute(event)
            self._refresh(event["id"])
            control = self._maintenance_control(event) if not target.exists() else None
            results.append(
                {
                    "event_id": event["id"],
                    "state": "complete" if target.exists() else control["state"],
                    "attempt": None if control is None else control["attempt"],
                }
            )
        return results

    async def _attribute(self, event):
        event_id = event["id"]
        target = self.root / "maintenance" / f"{event_id}.json"
        if target.exists():
            self._refresh(event_id)
            return
        control = self._maintenance_control(event)
        if control.get("legacy"):
            return
        attempt = control["attempt"]
        control_path = self.root / "maintenance" / f"{event_id}.control.json"
        request_path = self.root / "maintenance" / f"{event_id}.request.json"
        payload = (
            json.loads(request_path.read_text())
            if request_path.exists()
            else self._maintenance_payload(event)
        )
        if payload is None or len(json.dumps(payload, ensure_ascii=False)) > 35000:
            control["state"] = "oversized"
            atomic_json(control_path, control)
            return
        reservation_key = f"{event_id}:{attempt}"
        with counter_transaction(self.state_path) as state:
            reservations = state.setdefault("maintenance_reservations", {})
            allocation = reservations.get(reservation_key)
            if allocation is None:
                remaining = self.limit - state["reserved_calls"]
                if remaining <= 0:
                    control["state"] = "awaiting_budget"
                    atomic_json(control_path, control)
                    return
                reserved = min(self.config.protocol_attempts, remaining)
                allocation = {"reserved": reserved, "settled": False}
                reservations[reservation_key] = allocation
                state["reserved_calls"] += reserved
            reserved = allocation["reserved"]
        if not request_path.exists():
            atomic_json(request_path, payload)
        journal = StepJournal(
            self.root / "maintenance" / event_id / f"attempt-{attempt}.steps",
            workspace=self.service.workspace,
            bypass_cache=attempt > 0,
            provenance={
                "event_id": event_id,
                "attempt": attempt,
                "role": "wiki_maintainer",
                "producer_identity": event.get("producer_identity"),
                "evidence_refs": event["facts"].get("evidence_refs", []),
                "retry_reason": control.get("retry_reason"),
                "possible_duplicate_cost": control.get("possible_duplicate_cost", False),
            },
        )
        client = self.client_factory("optimization")
        session = ModelSession(
            client, self.config, "wiki_maintenance", limit=reserved, journal=journal
        )

        def valid(value):
            if set(value) != {"cause", "action", "training_ids"}:
                raise ValueError("Invalid Wiki attribution fields")
            if not all(
                isinstance(value[k], str) and len(value[k]) <= 1200 for k in ("cause", "action")
            ):
                raise ValueError("Invalid Wiki attribution text")
            if not isinstance(value["training_ids"], list) or (
                set(value["training_ids"]) - set(event["training_ids"])
            ):
                raise ValueError("Wiki attribution cites non-training evidence")
            return value

        try:
            response = await session.request(
                "wiki_maintainer",
                "只归因给定训练事实和运行契约；区分候选代码错误、框架覆盖缺口与服务故障。"
                "生成答案不等于标准答案，不可把某题答案写成规则；联合修改不能证明单项贡献。"
                "原因先作为假设，修法须描述通用规则并引用给定训练 ID。禁止猜测标准答案或修改分数。仅返回 JSON "
                '{"cause":"原因或待验证假设","action":"后续修法","training_ids":[]}',
                payload,
                valid,
                max_tokens=getattr(self.config, "wiki_max_tokens", None) or 1600,
            )
            atomic_json(
                target,
                {"attribution": response, "raw_outputs": session.raw, "events": session.events},
            )
            control["state"] = "complete"
        except Exception as exc:
            control["state"] = (
                "unknown"
                if isinstance(exc, UnknownRequest)
                else "awaiting_budget"
                if isinstance(exc, AwaitingBudget)
                else "abandoned"
                if isinstance(exc, RequestAbandoned)
                else "failed"
            )
            control["error"] = f"{type(exc).__name__}: {exc}"
            self._maintenance_failure(event_id, attempt, control["error"], session.events)
        finally:
            # Requests are reserved before the call. Unused slots return only after
            # an observable completion; a crash leaves the whole reservation charged.
            with counter_transaction(self.state_path) as state:
                allocation = state["maintenance_reservations"][reservation_key]
                if not allocation["settled"] and control["state"] != "awaiting_budget":
                    state["reserved_calls"] -= max(0, reserved - session.calls)
                    allocation.update(settled=True, calls=session.calls)
            atomic_json(control_path, control)
            await client.aclose()
            self._refresh(event_id)

    def _refresh(self, event_id):
        target = self.root / "maintenance" / f"{event_id}.json"
        if not target.exists():
            return
        wiki = self._wiki()
        for entry in wiki["entries"]:
            if entry["id"] == event_id and entry["pending_attribution"]:
                entry["attribution"] = json.loads(target.read_text())["attribution"]
                entry["pending_attribution"] = False
                wiki["version"] += 1
                wiki["lessons"] = _lessons(wiki["entries"])
                atomic_json(self.wiki_path, wiki)
                break
