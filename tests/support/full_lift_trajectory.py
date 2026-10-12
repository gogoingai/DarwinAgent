"""Recorded offline clients and read primitive for full LIFT transcript tests."""

import json
from types import SimpleNamespace


class RecordedWorker:
    def __init__(self):
        self.replies = iter(
            [
                {
                    "actions": [
                        {"tool": "read", "params": {"n": 1}},
                        {"tool": "read", "params": {"n": 2}},
                    ]
                },
                {"final_response": "first"},
                {"final_response": "corrected"},
            ]
        )

    async def chat(self, **kwargs):
        return SimpleNamespace(content=json.dumps(next(self.replies)), usage={})


def read_tool(params):
    if params["n"] == 2:
        raise ValueError("synthetic known read failure")
    return {"n": params["n"]}


class RecordedShared:
    def __init__(self, content='{"trajectory_score":0.75}', error=None):
        self.calls, self.content, self.error = 0, content, error

    def identity(self):
        return {"model": "synthetic", "budget": "same-ledger"}

    def scoped(self, session, role):
        assert role == "trajectory_judge"
        return self

    async def chat(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content, usage={"prompt_tokens": 1})


async def http_handler(mode, requests, request):
    """Return synthetic SSE or fail at the in-memory HTTP dispatch boundary."""
    import asyncio

    import httpx

    requests.append(json.loads(request.content))
    if mode == "unknown":
        raise httpx.ReadTimeout("synthetic dispatch timeout", request=request)
    if mode == "cancel":
        raise asyncio.CancelledError()
    if mode == "failed":
        return httpx.Response(
            400, json={"error": {"message": "synthetic", "type": "invalid_request_error"}}
        )
    chunk = {
        "id": "synthetic-score",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "synthetic",
        "choices": [
            {"index": 0, "delta": {"content": '{"trajectory_score":0.75}'}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
    )
