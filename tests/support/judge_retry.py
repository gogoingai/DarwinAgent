"""Offline HTTP responses for durable scorer and transport retry regressions."""

import json
from types import SimpleNamespace

import httpx

from darwinagent.config import Config
from datasets.lift.transport import SharedTransport


def offline_transport(root, outputs, *, minimax=False, client_factory):
    responses = iter(outputs)
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        output = next(responses)
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, int):
            return httpx.Response(
                output, json={"error": {"message": "synthetic", "type": "server_error"}}
            )
        chunk = dict(
            id="offline",
            object="chat.completion.chunk",
            created=1,
            model="synthetic",
            choices=[dict(index=0, delta={"content": output}, finish_reason="stop")],
            usage={"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
        )

    original = httpx.AsyncClient

    class OfflineClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    model = "MiniMax-M3.1-Flash-Preview" if minimax else "synthetic"
    cfg = Config(
        api_base_url="https://api.minimax.cn/v1" if minimax else "https://synthetic.invalid/v1",
        api_key="offline",
        model_strong=model,
        model_middle=model,
        model_fast=model,
        model_profiles=False,
        provider_request_policy="minimax-adaptive-v1" if minimax else "generic",
        thinking_type="adaptive",
        reasoning_effort="max",
        max_retries=1,
        max_http_requests=100,
        request_budget_path=root / "ledger.json",
        work_dir=root / "client",
    )
    cfg.role_tiers.update(worker="strong", judge="strong", trajectory_judge="strong")
    client = client_factory(cfg, OfflineClient)
    return client, SharedTransport(client, root / "shared"), requests


def recorded_scalar_shared(outputs):
    class Shared:
        def identity(self):
            return {"model": "synthetic", "budget": "unchanged"}

        def scoped(self, session, role):
            assert (session, role) == ("session", "trajectory_judge")
            return self

        async def chat(self, **kwargs):
            self.messages.append(kwargs)
            output = next(self.outputs)
            if isinstance(output, BaseException):
                raise output
            return SimpleNamespace(
                content=output, usage={"prompt_tokens": 10, "completion_tokens": 7}
            )

    shared = Shared()
    shared.outputs = iter(outputs)
    shared.messages = []
    return shared
