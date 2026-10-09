"""Minimal JSON ReAct loop. External adapters can use the context without this loop."""

import json
from dataclasses import dataclass

from darwinagent.contracts import plain

from .context import CheckRejected
from .failures import ToolFailure


@dataclass(frozen=True)
class ReActAgent:
    max_turns: int = 16
    role: str = "worker"
    system_prompt: str = (
        'Return a JSON object: {"tool": "registered name", "params": {...}} to act, '
        'or {"final_response": "text", "artifacts": []} to finish.'
    )

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
                        "view": context.view(),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        for _ in range(self.max_turns):
            response = await context.model(messages, role=self.role)
            action = json.loads(response.content)
            messages.append({"role": "assistant", "content": response.content})
            try:
                if set(action) <= {"final_response", "artifacts"} and isinstance(
                    action.get("final_response"), str
                ):
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
                if set(action) != {"tool", "params"}:
                    raise ValueError("Invalid ReAct action")
                output = await context.call(action["tool"], action["params"])
            except ToolFailure as exc:
                output = {"error": exc.to_dict()}
            except CheckRejected as exc:
                output = {"error": {"error_type": "CheckRejected", "message": str(exc)}}
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {"observation": plain(output), "view": context.view()}, ensure_ascii=False
                    ),
                }
            )
        return context.result(status="budget_exhausted")
