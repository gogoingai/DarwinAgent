"""Optional per-task budget, separate from the unchanged cumulative HTTP policy."""

import json
from dataclasses import dataclass

from darwinagent.llm.client import BudgetExceeded

from .rate_control import RateLimitedSessionTransport
from .task_control import TaskControlPolicy


@dataclass(frozen=True)
class TaskExecutionPolicy:
    correction_rounds: int
    worker_request_limit: int
    control: TaskControlPolicy | None = None

    def __post_init__(self):
        if (
            type(self.correction_rounds) is not int
            or not 1 <= self.correction_rounds <= 30
            or type(self.worker_request_limit) is not int
            or self.worker_request_limit < 1
        ):
            raise ValueError("Explicit positive task caps and 1-30 native rounds are required")
        if self.control is not None and (
            not isinstance(self.control, TaskControlPolicy)
            or self.control.delivery_reserve >= self.worker_request_limit
        ):
            raise ValueError("Invalid task control or delivery reserve")

    def declaration(self):
        value = dict(
            kind="bounded-task-v1",
            correction_rounds=self.correction_rounds,
            worker_request_limit=self.worker_request_limit,
            known_rejection_attempts=1,
        )
        if self.control is not None:
            value.update(kind="bounded-task-v2", control=self.control.declaration())
        return value

    @classmethod
    def from_declaration(cls, value):
        keys = {"kind", "correction_rounds", "worker_request_limit", "known_rejection_attempts"}
        controlled = isinstance(value, dict) and value.get("kind") == "bounded-task-v2"
        if controlled:
            keys.add("control")
        if (
            type(value) is not dict
            or set(value) != keys
            or value["kind"] != ("bounded-task-v2" if controlled else "bounded-task-v1")
            or type(value["known_rejection_attempts"]) is not int
            or value["known_rejection_attempts"] != 1
        ):
            raise ValueError("Invalid bounded task policy declaration")
        control = TaskControlPolicy.from_declaration(value["control"]) if controlled else None
        return cls(value["correction_rounds"], value["worker_request_limit"], control=control)


class TaskWorkerSession:
    def __init__(self, shared, session, policy, control=None):
        self.shared, self.session, self.policy = shared, session, policy
        self.control = control
        if policy.control is not None and control is None:
            raise ValueError("Controlled task policy requires its host retrieval control")
        self.view = RateLimitedSessionTransport(shared, session, "worker", max_attempts=1)

    def __getattr__(self, name):
        return getattr(self.view, name)

    def request_projection_identity(self):
        if self.control is None:
            return None
        return {
            "policy": self.policy.declaration(),
            "session": self.session,
            "audit_root": str(self.control.audit_path.resolve()),
        }

    def prepare_journal_request(self, journal, kwargs):
        if self.control is None:
            return kwargs
        saved = journal.root / f"{journal.position:06d}.json"
        if saved.exists():
            row = json.loads(saved.read_text())
            last = row["input"]["messages"][-1]
            snapshot = json.loads(last["content"])
            if last["role"] != "user" or set(snapshot) != {"execution_control"}:
                raise ValueError("Missing immutable task-control snapshot")
            message = last
        else:
            if not hasattr(self.shared, "_actual_worker_attempts"):
                self.shared.check()
            used = self.shared._actual_worker_attempts.get(self.session, 0)
            message = {
                "role": "user",
                "content": json.dumps(
                    {"execution_control": self.control.snapshot(used=used)}, ensure_ascii=False
                ),
            }
        return dict(kwargs, messages=[*kwargs["messages"], message])

    async def chat(self, **kwargs):
        with self.shared.campaign_lease():
            self.shared.check()
            used = self.shared._actual_worker_attempts.get(self.session, 0)
            if used >= self.policy.worker_request_limit:
                raise BudgetExceeded("Cumulative task execution request budget exhausted")
            # The campaign lease remains held through the single-send transport.
            # Its existing durable worker counter reserves before dispatch. Use
            # retained HTTP receipts for the cap: a proven zero-send resource
            # refusal must not consume a paid-send slot. No counter is reset. A
            # rejected cap creates neither a receipt nor a pending service intent.
            if self.control is not None:
                snapshot = json.loads(kwargs["messages"][-1]["content"])["execution_control"]
                if (
                    snapshot["task_model"]["used"] != used
                    or snapshot["task_model"]["limit"] != self.policy.worker_request_limit
                ):
                    raise BudgetExceeded("Task control snapshot changed before dispatch")
            before = self.shared.client.http_attempts()
            try:
                return await self.view.chat(**kwargs)
            finally:
                self.shared._actual_worker_attempts[self.session] = (
                    used + self.shared.client.http_attempts() - before
                )


def scope_task_worker(shared, session, role, policy, *, control=None):
    if role == "worker":
        return TaskWorkerSession(shared, session, policy, control=control)
    return RateLimitedSessionTransport(shared, session, role, max_attempts=1)
