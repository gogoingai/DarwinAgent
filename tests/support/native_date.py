"""Recorded judge response for calendar replay; no transport or provider access."""

import json
from types import SimpleNamespace


class RecordedJudgeClient:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content=json.dumps(
                {
                    "final_response": json.dumps(
                        {"success": False, "score": 0.5, "reason": "one improvement"}
                    )
                }
            ),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            elapsed_s=0.0,
        )
