"""Training-only optimization memory. Facts are durable before model attribution."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from darwinagent.agents.protocol import ModelSession
from darwinagent.contracts import freeze
from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import BUILTINS, DATA_CAPABILITIES
from darwinagent.runtime.artifacts import atomic_json, digest

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
            source_refs=original_refs,
        )
        facts["evidence_refs"] = [raw_ref, *original_refs]
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

    async def _attribute(self, event):
        target = self.root / "maintenance" / f"{event['id']}.json"
        if target.exists():
            return
        request_path = self.root / "maintenance" / f"{event['id']}.request.json"
        if request_path.exists():
            # A lost reply might already have consumed tokens. Never replay it silently.
            return
        state = json.loads(self.state_path.read_text())
        if state["reserved_calls"] >= self.limit:
            return
        remaining = self.limit - state["reserved_calls"]
        reserved = min(self.config.protocol_attempts, remaining)
        payload = {
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
            atomic_json(
                self.root / "maintenance" / f"{event['id']}.failure.json",
                {"error": "Maintenance evidence exceeds 35000 characters"},
            )
            return
        atomic_json(request_path, payload)
        state["reserved_calls"] += reserved
        atomic_json(self.state_path, state)
        client = self.client_factory("optimization")
        session = ModelSession(client, self.config, "wiki_maintenance", limit=reserved)

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
        except Exception as exc:
            atomic_json(
                self.root / "maintenance" / f"{event['id']}.failure.json",
                {"error": f"{type(exc).__name__}: {exc}", "events": session.events},
            )
        finally:
            # Requests are reserved before the call. Unused slots return only after
            # an observable completion; a crash leaves the whole reservation charged.
            state = json.loads(self.state_path.read_text())
            state["reserved_calls"] -= reserved - session.calls
            atomic_json(self.state_path, state)
            await client.aclose()
            self._refresh(event["id"])

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
