"""Synthetic persistent ledger client; no HTTP or provider construction."""

import json
from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import Config
from darwinagent.runtime.artifacts import atomic_json


class FakeLLMClient:
    def __init__(self, root, *, full=False, unknown=False):
        model = "MiniMax-M3.1-Flash-Preview"
        self.cfg = Config(
            api_base_url="https://api.minimax.cn/v1",
            api_key="offline-dummy",
            model_strong=model,
            model_fast=model,
            model_middle=model,
            max_retries=1,
            max_concurrency=1,
            max_http_requests=None,
            request_budget_path=Path(root) / "http-attempts.json",
            provider_request_policy="minimax-adaptive-v1",
            thinking_type="adaptive",
            reasoning_effort="max",
            limits={},
        )
        self.cfg.role_tiers.update(worker="strong", judge="strong")
        if full:
            self.cfg.role_tiers["trajectory_judge"] = "strong"
        self.calls = []
        self.unknown = unknown

    def http_attempts(self):
        path = self.cfg.request_budget_path
        return json.loads(path.read_text())["attempts"] if path.exists() else 0

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        atomic_json(self.cfg.request_budget_path, {"attempts": self.http_attempts() + 1})
        if self.unknown:
            raise ConnectionError("synthetic unknown")
        return SimpleNamespace(content="synthetic", usage={"total_tokens": 1})
