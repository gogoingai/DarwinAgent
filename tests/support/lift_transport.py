"""Offline recorded transport for shared LIFT/runtime behavior tests."""

from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import Config


class Recorded:
    def __init__(self, root, unknown=False):
        self.cfg = Config(
            model_strong="recorded",
            max_http_requests=100,
            work_dir=root,
            request_budget_path=Path(root).parent / "http-attempts.json",
        )
        self.calls, self.unknown = 0, unknown

    def http_attempts(self):
        return 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.unknown:
            raise ConnectionError("recorded unknown submission")
        return SimpleNamespace(content="ok", usage={})
