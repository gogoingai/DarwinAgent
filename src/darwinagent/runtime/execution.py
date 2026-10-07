"""Explicit execution scope and previews, independent of datasets and agents."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .artifacts import atomic_json

STAGES = frozenset(
    {
        "facts",
        "vector",
        "graph",
        "retrieval",
        "answer",
        "check",
        "review",
        "score",
        "proposal",
        "candidate_check",
        "wiki",
        "report",
    }
)
MODES = frozenset({"continue", "retry_failed", "rerun", "fork", "rerun_all"})


@dataclass(frozen=True)
class ExecutionSelection:
    mode: str = "continue"
    case_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    stages: tuple[str, ...] = ("facts", "graph", "retrieval", "answer", "check", "review", "score")
    branch: str = "main"
    strict: bool = False
    candidate: str | None = None

    def __post_init__(self):
        if self.mode not in MODES or set(self.stages) - STAGES:
            raise ValueError("Unknown resume mode or execution stage")
        if not self.branch or "/" in self.branch or ".." in self.branch:
            raise ValueError("Invalid workspace branch")

    def includes(self, case_id, question_id=None):
        return (not self.case_ids or case_id in self.case_ids) and (
            question_id is None or not self.question_ids or question_id in self.question_ids
        )

    def to_dict(self):
        return asdict(self)


@dataclass
class ExecutionPlan:
    reuse: list = field(default_factory=list)
    execute: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    selection: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class ActiveBudget:
    """Only explicitly entered active intervals consume time; offline time is free."""

    def __init__(self, path, limit_s, clock=time.monotonic):
        if limit_s <= 0:
            raise ValueError("Budget must be positive")
        self.path, self.limit_s, self.clock = Path(path), limit_s, clock
        self.spent = (
            json.loads(self.path.read_text()).get("spent_s", 0) if self.path.exists() else 0
        )
        self.started = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._heartbeat = None

    def checkpoint(self):
        """Persist active time, never charge time since the last crashed heartbeat."""
        with self._lock:
            if self.started is not None:
                now = self.clock()
                self.spent += max(0, now - self.started)
                self.started = now
            atomic_json(self.path, {"limit_s": self.limit_s, "spent_s": self.spent})

    def _tick(self):
        while not self._stop.wait(1.0):
            self.checkpoint()

    def __enter__(self):
        self.started = self.clock()
        atomic_json(self.path, {"limit_s": self.limit_s, "spent_s": self.spent})
        self._stop.clear()
        self._heartbeat = threading.Thread(target=self._tick, daemon=True)
        self._heartbeat.start()
        return self

    @property
    def remaining(self):
        self.checkpoint()
        return max(0, self.limit_s - self.spent)

    def __exit__(self, *args):
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join()
        self.checkpoint()
        self.started = None
