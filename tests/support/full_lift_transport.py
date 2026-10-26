"""Synthetic persistent ledger client; no HTTP or provider construction."""

import json
from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import Config
from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.full_transport import FullSharedTransport
from datasets.lift.transport import SharedTransport


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


class FullTransportFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.shared_root = self.root / "shared"
        self.old = FakeLLMClient(self.root)
        SharedTransport(self.old, self.shared_root, worker_limit=None)
        atomic_json(self.root / "http-attempts.json", {"attempts": 307})
        atomic_json(
            self.shared_root / "requests/original.json",
            {
                "status": "completed",
                "actual_http_attempts": 307,
                "session": "historic-judge",
                "run_id": "original-run",
                "role": "judge",
                "output": "old evidence",
            },
        )
        atomic_json(self.shared_root / "worker-calls.json", {"historic-worker": 8})
        self.identity = {
            "run_id": "fresh-full",
            "source_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        }
        self.client = FakeLLMClient(self.root, full=True)
        self.original_bytes = (self.shared_root / "policy.json").read_bytes()
        self.receipt_bytes = (self.shared_root / "requests/original.json").read_bytes()

    def full(self, *, enroll=True, client=None, identity=None):
        return FullSharedTransport(
            client or self.client,
            self.shared_root,
            worker_limit=None,
            enroll_role=enroll,
            experiment_identity=identity or self.identity,
        )
