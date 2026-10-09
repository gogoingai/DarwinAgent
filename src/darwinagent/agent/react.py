"""Minimal JSON ReAct loop. External adapters can use the context without this loop."""

import json
from dataclasses import dataclass

from darwinagent.contracts import plain


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
            if set(action) <= {"final_response", "artifacts"} and isinstance(
                action.get("final_response"), str
            ):
                return context.result(
                    action["final_response"], artifacts=action.get("artifacts", ())
                )
            if set(action) != {"tool", "params"}:
                raise ValueError("Invalid ReAct action")
            output = await context.call(action["tool"], action["params"])
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {"observation": plain(output), "view": context.view()}, ensure_ascii=False
                    ),
                }
            )
        return context.result(status="budget_exhausted")
