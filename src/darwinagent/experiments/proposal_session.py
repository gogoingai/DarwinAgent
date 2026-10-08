"""Durable proposal dialogue, including on-demand Wiki evidence requests."""

from __future__ import annotations

import json
from pathlib import Path

from darwinagent.agents.protocol import ProtocolError, parse_json
from darwinagent.runtime.artifacts import atomic_json, digest

ACTION_GUIDANCE = """输出必须是一个 JSON 对象，必须有 action 字段；不要用 query_wiki 等动作名称作为顶层键。
合法查询示例：{"action":"query_wiki","query":{"question":"核对原件与反证","scope":{"training_ids":["系统提供的 training_id"]},"view":"regroup","cursor":null,"max_chars":8000}}
题目的 wiki_scope 可直接作为查询 scope；training_ids 使用系统提供的完整组合编号。question_ids 使用局部 question_id，或兼容完整 training_id；跨对话同名题号应优先用 training_ids 精确定位。
首次查询 cursor 必须为 null；后续 cursor 只能原样使用 Wiki 返回的字符串，不能用数字页码。
提交示例：{"action":"submit_patch","patches":[...]}
无修改示例：{"action":"no_change","reason":"当前证据不足","unresolved":["待查问题"]}
字段含义与训练范围仍以系统提供的输入为准。"""


class ProposalSession:
    """Resume saved responses; an in-flight request without a receipt stays unknown."""

    def __init__(
        self, target, *, payload, protocol, call_limit=None, previous=None, workspace=None
    ):
        self.target = Path(target)
        self.call_limit = call_limit
        self.workspace = workspace
        protocol = protocol + "\n" + ACTION_GUIDANCE
        if self.target.exists():
            self.state = json.loads(self.target.read_text())
            if self.state["input"]["base_version"] != payload["base_version"]:
                raise ValueError("Proposal session baseline changed; fork a new session")
            if self.state.get("protocol") != protocol:
                self.state["pending_protocol"] = protocol
                self.save()
        else:
            messages = [
                {"role": "system", "content": protocol},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            calls = 0
            if previous is not None and Path(previous).exists():
                prior = json.loads(Path(previous).read_text())
                if prior["input"]["base_version"] != payload["base_version"]:
                    raise ValueError("Cannot continue a proposal against a different baseline")
                messages = prior.get("messages", messages)
                messages = messages + [
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
                ]
                calls = prior.get("calls", 0)
            self.state = dict(
                input=payload,
                protocol=protocol,
                messages=messages,
                raw_outputs=[],
                events=[],
                calls=calls,
                phase="ready",
                exchanges=[],
                format_failures=0,
            )
            self.save()

    def save(self):
        atomic_json(self.target, self.state)

    def feedback(self, error):
        """Retain the rejected draft and continue the same frozen-baseline dialogue."""
        self.state["messages"].append(
            {
                "role": "user",
                "content": "候选准入失败：" + str(error) + "。保留草稿，必要时查询 Wiki 后修正。",
            }
        )
        self.state.pop("action", None)
        self.state.update(phase="ready", format_failures=0)
        self.save()

    def recover_response(self, content):
        if self.state["phase"] != "submitted":
            raise ValueError("Only an unresolved submitted request can receive a receipt")
        atomic_json(
            self.target.with_name(self.target.name + ".receipt.json"),
            {"request_id": self.state["request_id"], "content": content},
        )

    def retry_unknown(self, reason):
        if self.state["phase"] != "submitted" or not str(reason).strip():
            raise ValueError("Explicit reason required to retry an unknown request")
        self.state["events"].append(
            {
                "status": "human_retry_unknown",
                "request_id": self.state["request_id"],
                "reason": reason,
                "possible_duplicate_cost": True,
            }
        )
        self.state["phase"] = "ready"
        self.save()

    async def run(self, client, config, decoder, wiki_service=None):
        while True:
            phase = self.state["phase"]
            if self.state.get("pending_protocol") and phase in ("ready", "query"):
                new_protocol = self.state.pop("pending_protocol")
                self.state["events"].append(
                    {
                        "status": "protocol_intervention",
                        "previous_protocol": self.state["protocol"],
                        "new_protocol": new_protocol,
                    }
                )
                self.state["protocol"] = new_protocol
                self.state["messages"][0] = {"role": "system", "content": new_protocol}
                self.state["messages"].append(
                    {
                        "role": "user",
                        "content": "协议已修订，保留此前响应和查询历史。" + ACTION_GUIDANCE,
                    }
                )
                self.state["format_failures"] = 0
                self.save()
            if phase == "finished":
                return decoder(self.state["action"])
            if phase == "query":
                action = self.state["action"]
                query_receipt = self.target.with_name(self.target.name + ".query-receipt.json")
                query_id = digest(
                    {"request_id": self.state["request_id"], "query": action["query"]}
                )
                saved_reply = (
                    json.loads(query_receipt.read_text()) if query_receipt.exists() else {}
                )
                if saved_reply.get("query_id") == query_id:
                    reply = saved_reply["reply"]
                elif wiki_service is None:
                    reply = {"status": "failed", "error": "Wiki query service unavailable"}
                else:
                    from .wiki_service import WikiQuery

                    # Query and reply are saved before advancing the dialogue. WikiService
                    # independently persists regroup work and its pinned evidence snapshot.
                    try:
                        result = await wiki_service.query(WikiQuery(**action["query"]))
                        reply = result.to_dict() if hasattr(result, "to_dict") else result
                    except (ValueError, TypeError, KeyError, OSError) as exc:
                        reply = {
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "suggestion": "Keep question/scope/view unchanged with a cursor; use null for a new query.",
                        }
                        for exchange in reversed(self.state["exchanges"]):
                            if (
                                action["query"].get("cursor")
                                and exchange["reply"].get("cursor") == action["query"]["cursor"]
                            ):
                                reply["retry_query"] = {
                                    **exchange["query"],
                                    "cursor": action["query"]["cursor"],
                                }
                                break
                atomic_json(query_receipt, {"query_id": query_id, "reply": reply})
                self.state["exchanges"].append({"query": action["query"], "reply": reply})
                context = {"wiki_reply": reply}
                if reply.get("cursor"):
                    context["continuation_query"] = {**action["query"], "cursor": reply["cursor"]}
                self.state["messages"].append(
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
                )
                self.state.update(phase="ready", format_failures=0)
                self.state.pop("action", None)
                self.save()
                continue
            receipt = self.target.with_name(self.target.name + ".receipt.json")
            if phase == "submitted" and self.workspace is not None:
                replacement = self.workspace.replacement_for(self.state["request_id"])
                if replacement is not None:
                    self.state["request_id"] = replacement["id"]
                    if replacement["status"] == "prepared":
                        self.state["phase"] = "replacement_prepared"
                        self.save()
                        continue
                    if replacement.get("response_ref"):
                        recovered = self.workspace.read_json(replacement["response_ref"])
                        atomic_json(
                            receipt,
                            {"request_id": replacement["id"], "content": recovered["content"]},
                        )
                    self.save()
                request = self.workspace.request(self.state["request_id"])
                if request["status"] == "abandoned" and replacement is None:
                    self.state.update(
                        phase="finished",
                        action={
                            "action": "no_change",
                            "reason": "请求已由人工明确放弃",
                            "unresolved": [],
                        },
                    )
                    self.state["events"].append(
                        {"status": "abandoned", "request_id": request["id"]}
                    )
                    self.save()
                    continue
            if phase == "submitted":
                if not receipt.exists() and self.workspace is not None:
                    request = self.workspace.request(self.state["request_id"])
                    if request.get("response_ref"):
                        recovered = self.workspace.read_json(request["response_ref"])
                        atomic_json(
                            receipt,
                            {
                                "request_id": self.state["request_id"],
                                "content": recovered["content"],
                            },
                        )
                if not receipt.exists():
                    raise ProtocolError(
                        "Proposal request outcome unknown; recover or explicitly abandon it",
                        self.state["raw_outputs"],
                    )
                saved = json.loads(receipt.read_text())
                if saved["request_id"] != self.state["request_id"]:
                    raise ProtocolError(
                        "Proposal request outcome unknown; receipt belongs to another request"
                    )
                raw = saved["content"]
            else:
                if self.call_limit is not None and self.state["calls"] >= self.call_limit:
                    raise ProtocolError(
                        "Proposal call budget exhausted; increase budget to continue",
                        self.state["raw_outputs"],
                    )
                boundary = getattr(client, "_control_boundary", None)
                if boundary is not None:
                    boundary("model:" + config.proposal_role)
                self.state["calls"] += 1
                if phase != "replacement_prepared":
                    self.state["request_id"] = digest(
                        {
                            "base": self.state["input"]["base_version"],
                            "session": str(self.target.resolve()),
                            "call": self.state["calls"],
                            "messages": self.state["messages"],
                        }
                    )
                    if self.workspace is not None:
                        self.workspace.prepare_request(
                            {
                                "role": config.proposal_role,
                                "messages": self.state["messages"],
                                "base_version": self.state["input"]["base_version"],
                                "session": str(self.target),
                            },
                            request_id=self.state["request_id"],
                        )
                self.state["phase"] = "submitted"
                self.save()
                if self.workspace is not None:
                    self.workspace.submit_request(self.state["request_id"])
                try:
                    response = await client.chat(
                        role=config.proposal_role,
                        messages=[dict(message) for message in self.state["messages"]],
                        temperature=config.temperature,
                        max_tokens=14000,
                        json_mode=True,
                        namespace="proposal",
                        use_cache=False,
                        durable=True,
                    )
                except Exception as exc:
                    from darwinagent.llm.client import BudgetExceeded

                    if getattr(exc, "continuation_signal", False) or (
                        isinstance(exc, BudgetExceeded) and not exc.dispatched
                    ):
                        if self.workspace is not None:
                            self.workspace.defer_request(self.state["request_id"], reason=str(exc))
                        self.state["phase"] = "replacement_prepared"
                        self.state["calls"] -= 1
                        self.save()
                        raise
                    self.state["events"].append(
                        {
                            "status": "unknown",
                            "error": str(exc),
                            "request_id": self.state["request_id"],
                        }
                    )
                    self.save()
                    raise ProtocolError(
                        "Proposal request outcome unknown; recover or explicitly retry",
                        self.state["raw_outputs"],
                    ) from exc
                raw = response.content
                atomic_json(
                    receipt,
                    {
                        "request_id": self.state["request_id"],
                        "content": raw,
                        "requested_model": getattr(response, "requested_model", None)
                        or getattr(response, "model", None),
                        "provider_model": getattr(response, "provider_model", None),
                        "response_id": getattr(response, "response_id", None),
                        "usage": getattr(response, "usage", {}),
                    },
                )
            if self.workspace is not None:
                request = self.workspace.request(self.state["request_id"])
                if not request.get("response_ref"):
                    self.workspace.record_response(
                        self.state["request_id"],
                        {"content": raw},
                        metadata={
                            "role": config.proposal_role,
                            "provider_model": json.loads(receipt.read_text()).get("provider_model")
                            or "unknown",
                            "requested_model": json.loads(receipt.read_text()).get(
                                "requested_model"
                            ),
                            "response_id": json.loads(receipt.read_text()).get("response_id"),
                            "usage": json.loads(receipt.read_text()).get("usage", {}),
                        },
                    )
            self.state["raw_outputs"].append(raw)
            self.state["messages"].append({"role": "assistant", "content": raw})
            try:
                action = decode_action(parse_json(raw))
                decoder(action)  # Validate patches before committing a terminal action.
            except (ValueError, TypeError, KeyError) as exc:
                self.state["format_failures"] += 1
                self.state["events"].append({"status": "protocol_error", "error": str(exc)})
                self.state["messages"].append(
                    {
                        "role": "user",
                        "content": "协议或执行校验失败："
                        + str(exc)
                        + "。请重新输出完整 JSON。\n"
                        + ACTION_GUIDANCE,
                    }
                )
                self.state["phase"] = "ready"
                self.save()
                if self.state["format_failures"] >= config.protocol_attempts:
                    raise ProtocolError(str(exc), self.state["raw_outputs"]) from exc
                continue
            self.state["events"].append({"status": "ok", "action": action["action"]})
            self.state["action"] = action
            self.state["phase"] = "query" if action["action"] == "query_wiki" else "finished"
            self.save()


def decode_action(obj):
    if set(obj) == {"patches"}:
        return {"action": "submit_patch", "patches": obj["patches"]}
    action = obj.get("action")
    if action == "submit_patch" and set(obj) == {"action", "patches"}:
        return obj
    if action == "no_change" and set(obj) <= {"action", "reason", "unresolved"}:
        if not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
            raise ValueError("no_change requires a nonempty reason")
        return obj
    if action == "query_wiki" and set(obj) == {"action", "query"}:
        query = obj["query"]
        if not isinstance(query, dict) or set(query) - {
            "question",
            "scope",
            "view",
            "cursor",
            "max_chars",
        }:
            raise ValueError("Invalid Wiki query fields")
        if not isinstance(query.get("question"), str) or not query["question"].strip():
            raise ValueError("Wiki query requires a nonempty question")
        if query.get("view", "raw") not in ("raw", "summary", "regroup"):
            raise ValueError("Invalid Wiki query view")
        if not isinstance(query.get("scope", {}), dict):
            raise ValueError("Wiki query scope must be an object")
        if query.get("cursor") is not None and not isinstance(query["cursor"], str):
            raise ValueError("Wiki cursor must be null for a first query or a returned string")
        if type(query.get("max_chars", 8000)) is not int or query.get("max_chars", 8000) < 512:
            raise ValueError("Wiki query size must be at least 512")
        return obj
    raise ValueError("Expected query_wiki, submit_patch or no_change")
