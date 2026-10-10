"""Minimal JSON ReAct loop. External adapters can use the context without this loop."""

import json
from dataclasses import dataclass, field

from darwinagent.contracts import plain
from darwinagent.kernel.spec import validate_value

from .context import CheckRejected
from .failures import ToolFailure

_ACTION_PROTOCOL = (
    'Return exactly one JSON object: {"tool": "registered name", "params": {...}} to act, '
    'or {"actions":[{"tool":"registered name","params":{...}}, ...]} for 1-16 independent actions, '
    'or {"final_response": "text", "artifacts": [{"path": "actual output path"}]} to finish. '
    "Tool must be a nonempty string and params an object. Final_response must be a string. "
    "Artifacts is optional; when present it must be an array of JSON objects, never strings. "
    "Use [] for no artifacts. For files, copy the actual path from a completed tool result; "
    "include other metadata only when supported by that result. Do not invent files or receipts. "
    "A batch runs sequentially through the same checked tools and stops on a known failure; "
    "every primitive call still counts. Batch related reads to leave model requests for "
    "computing, writing and final delivery within the host's fixed budget. "
    "Other artifact objects follow the host artifact contract; no universal metadata field "
    "is required. Do not repeat a completed write just to correct the final response format."
)


class ActionProtocolError(ValueError):
    """A settled reply violates the ReAct envelope, before any runtime operation."""


def _validate_action(action):
    if not isinstance(action, dict):
        raise ActionProtocolError("Reply must be a JSON object.")
    if "final_response" in action and set(action) <= {"final_response", "artifacts"}:
        if not isinstance(action["final_response"], str):
            raise ActionProtocolError("final_response must be a string.")
        artifacts = action.get("artifacts", [])
        if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
            raise ActionProtocolError("artifacts must be an array of JSON objects, not strings.")
        return
    if set(action) == {"actions"}:
        actions = action["actions"]
        if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
            raise ActionProtocolError("actions must be an array of 1-16 tool/params objects.")
        for item in actions:
            if not isinstance(item, dict) or set(item) != {"tool", "params"}:
                raise ActionProtocolError("Every batch item must contain exactly tool/params.")
            _validate_action(item)
        return
    if set(action) == {"tool", "params"}:
        if not isinstance(action["tool"], str) or not action["tool"].strip():
            raise ActionProtocolError("tool must be a nonempty string.")
        if not isinstance(action["params"], dict):
            raise ActionProtocolError("params must be a JSON object.")
        return
    raise ActionProtocolError(
        "Use tool/params, actions, or final_response with optional artifacts."
    )


def _validate_batch(context, actions):
    """Validate all registrations and input contracts before the first effect."""
    contracts = {tool["name"]: tool["input_contract"] for tool in context.tool_descriptions()}
    for action in actions:
        if action["tool"] not in contracts:
            raise ActionProtocolError("Batch tool is not registered: " + action["tool"])
        try:
            validate_value(action["params"], contracts[action["tool"]], "batch.params")
        except ValueError as exc:
            raise ActionProtocolError(str(exc)) from exc


async def _run_batch(context, actions):
    observations = []
    for index, action in enumerate(actions):
        stopped = False
        try:
            output = await context.call(action["tool"], action["params"])
        except ToolFailure as exc:
            output, stopped = {"error": exc.to_dict()}, True
        except CheckRejected as exc:
            output, stopped = {"error": {"error_type": "CheckRejected", "message": str(exc)}}, True
        observations.append({**action, "observation": plain(output)})
        if stopped:
            return {
                "actions": observations,
                "stopped": True,
                "unexecuted_actions": len(actions) - index - 1,
            }
    return {"actions": observations, "stopped": False, "unexecuted_actions": 0}


def _current_view(messages, previous, task, context):
    """Replace only our structured snapshot; observations and raw context stay intact."""
    if previous is not None:
        payload = json.loads(messages[previous]["content"])
        payload.pop("view")
        messages[previous]["content"] = json.dumps(payload, ensure_ascii=False)
    view = context.view()
    # The raw no-schema view repeats the exact task already supplied once. A
    # schema's materialized fields are semantic state and must never be trimmed.
    if view.get("task") == task.to_dict():
        view = {k: v for k, v in view.items() if k != "task"}
    position = len(messages) - 1
    payload = json.loads(messages[position]["content"])
    payload["view"] = view
    messages[position]["content"] = json.dumps(payload, ensure_ascii=False)
    return position


@dataclass(frozen=True)
class ReActAgent:
    max_turns: int = 16
    role: str = "worker"
    system_prompt: str = _ACTION_PROTOCOL
    max_tokens: int | None = None
    view_layout: str = field(default="latest-view-v1", init=False)

    def __post_init__(self):
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer or None")

    async def run(self, task, context):
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": task.to_dict(),
                        "prompt": context.prompt(self.role),
                        "tools": context.tool_descriptions(),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        view_position = None
        for _ in range(self.max_turns):
            view_position = _current_view(messages, view_position, task, context)
            generation = {} if self.max_tokens is None else {"max_tokens": self.max_tokens}
            response = await context.model(messages, role=self.role, json_mode=True, **generation)
            messages.append({"role": "assistant", "content": response.content})
            try:
                action = json.loads(response.content)
                _validate_action(action)
                if "actions" in action:
                    _validate_batch(context, action["actions"])
            except (json.JSONDecodeError, ActionProtocolError) as exc:
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "observation": {
                                    "error": {
                                        "error_type": type(exc).__name__,
                                        "message": (
                                            "Reply was not valid JSON. "
                                            if isinstance(exc, json.JSONDecodeError)
                                            else str(exc) + " "
                                        )
                                        + "No action was executed for this reply. "
                                        + _ACTION_PROTOCOL,
                                    }
                                }
                            }
                        ),
                    }
                )
                continue
            try:
                if "final_response" in action:
                    result = context.result(
                        action["final_response"], artifacts=action.get("artifacts", ())
                    )
                    context.check(
                        "final_delivery",
                        {
                            "status": result.status,
                            "final_response": result.final_response,
                            "artifacts": result.to_dict()["artifacts"],
                        },
                    )
                    return result
                output = (
                    await _run_batch(context, action["actions"])
                    if "actions" in action
                    else await context.call(action["tool"], action["params"])
                )
            except ToolFailure as exc:
                output = {"error": exc.to_dict()}
            except CheckRejected as exc:
                output = {"error": {"error_type": "CheckRejected", "message": str(exc)}}
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps({"observation": plain(output)}, ensure_ascii=False),
                }
            )
        return context.result(status="budget_exhausted")
