"""Recorded offline clients and read primitive for full LIFT transcript tests."""

import json
from types import SimpleNamespace

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent, ToolSpec
from darwinagent.runtime.artifacts import atomic_json, digest


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


async def recorded_transcript(root, *, macro=False):
    from darwinagent.agent import AgentAsset, AgentKernelAssets

    profile = ExecutionProfile("test", ("read",))
    kernel = None
    worker = RecordedWorker()
    if macro:
        kernel = AgentKernelAssets(
            (
                AgentAsset(
                    "schema",
                    "S",
                    json.dumps(
                        {
                            "nodes": [
                                {
                                    "id": "Task",
                                    "source": "task",
                                    "where": {},
                                    "fields": {
                                        "instruction": {"path": "instruction", "type": "string"}
                                    },
                                }
                            ],
                            "relations": [],
                        }
                    ),
                ),
                AgentAsset(
                    "twice",
                    "F",
                    'def run(params):\n    first = call("read", {"n": 1})\n    second = call("read", {"n": 2})\n    return {"first":first,"second":second}\n',
                    trial_inputs=({},),
                    schema_dependencies=("schema",),
                ),
                AgentAsset(
                    "worker",
                    "P",
                    "$instruction\n$tools\n$view",
                    role="worker",
                    schema_dependencies=("schema",),
                ),
            ),
            profile,
        ).export(root / "kernel")
        worker.replies = iter(
            [
                {"tool": "twice", "params": {}},
                {"final_response": "first"},
                {"final_response": "corrected"},
            ]
        )
    atomic_json(
        root / "session-identity.json",
        {
            "role": "worker",
            "task_id": "scene/q1",
            "kernel_id": kernel.content_id if kernel else None,
            "session": "s",
        },
    )
    backend = ExecutionBackend(
        ReActAgent(),
        [ToolSpec("read", read_tool)],
        profile,
        worker,
        root / "episodes",
    )
    history, turns = [], []
    for i, prompt in enumerate(["native task", "native correction"]):
        history.append({"role": "user", "content": prompt})
        task = AgentTask("scene/q1", "synthetic", parameters={"native_messages": history})
        ep = (await backend.run(task, kernel, run_id=f"turn-{i}")).to_dict()
        receipt = {"message_hash": digest(prompt), "status": ep["status"], "episode": ep}
        atomic_json(root / "turns" / f"{i}.json", receipt)
        turns.append(receipt)
        history.append({"role": "assistant", "content": ep["final_response"]})
    atomic_json(root / "chat.json", {"history": history, "turns": turns})
