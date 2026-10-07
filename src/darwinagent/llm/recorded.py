"""Explicit recorded transport for offline framework tests; never an experiment fallback."""

import json
from collections import deque
from types import SimpleNamespace


class RecordedClient:
    def __init__(self, replies):
        self.replies = {role: deque(values) for role, values in replies.items()}
        self.calls = []

    async def chat(self, **request):
        self.calls.append(request)
        role = request["role"]
        if role not in self.replies or not self.replies[role]:
            raise RuntimeError("Recorded replies exhausted for " + role)
        result = self.replies[role].popleft()
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(
            content=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        )
