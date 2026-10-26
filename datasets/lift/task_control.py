"""Task-wide operational feedback and a receipt-backed retrieval stop policy."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from darwinagent.agent.failures import ToolRejectedBeforeEffect

RETRIEVAL_TOOLS = frozenset({"read_web", "search_web"})


class RetrievalStopped(ToolRejectedBeforeEffect):
    public_codes = frozenset(
        {"retrieval_limit_reached", "delivery_reserve_reached", "retrieval_tool_failure_limit"}
    )


@dataclass(frozen=True)
class TaskControlPolicy:
    retrieval_limit: int = 12
    delivery_reserve: int = 4
    max_failures_per_tool: int = 3

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()):
            raise ValueError("Task control limits must be positive integers")

    def declaration(self):
        return dict(kind="task-control-v1", **asdict(self))

    @classmethod
    def from_declaration(cls, value):
        if (
            type(value) is not dict
            or set(value)
            != {"kind", "retrieval_limit", "delivery_reserve", "max_failures_per_tool"}
            or value["kind"] != "task-control-v1"
        ):
            raise ValueError("Invalid task control declaration")
        return cls(**{k: v for k, v in value.items() if k != "kind"})


class TaskControl:
    def __init__(self, shared, session, limit, policy, audit_path):
        if not isinstance(policy, TaskControlPolicy) or not 0 < policy.delivery_reserve < limit:
            raise ValueError("Delivery reserve must be below the execution request limit")
        self.shared, self.session, self.limit = shared, session, limit
        self.policy, self.audit_path = policy, Path(audit_path)

    def snapshot(self, *, used):
        calls, failures = 0, {name: 0 for name in RETRIEVAL_TOOLS}
        for path in self.audit_path.glob("*.json"):
            row = json.loads(path.read_text())
            if row.get("name") in RETRIEVAL_TOOLS:
                calls += 1
                if row["output"].get("ok") is not True:
                    failures[row["name"]] += 1
        remaining = max(0, self.limit - used)
        closed = remaining <= self.policy.delivery_reserve or calls >= self.policy.retrieval_limit
        blocked = sorted(
            name
            for name in RETRIEVAL_TOOLS
            if closed or failures[name] >= self.policy.max_failures_per_tool
        )
        return {
            "policy": self.policy.declaration(),
            "task_model": {"used": used, "limit": self.limit, "remaining": remaining},
            "retrieval": {
                "used": calls,
                "limit": self.policy.retrieval_limit,
                "remaining": max(0, self.policy.retrieval_limit - calls),
                "failures": failures,
            },
            "phase": "delivery" if closed or len(blocked) == len(RETRIEVAL_TOOLS) else "research",
            "blocked_tools": blocked,
            "instruction": "Use collected evidence to modify and deliver the requested artifact. Report unverified claims or missing evidence honestly; do not invent successful verification."
            if closed or len(blocked) == len(RETRIEVAL_TOOLS)
            else "Gather only the evidence needed for this task. Stop once sufficient; preserve requests for modification and final delivery.",
        }

    def before_tool(self, name):
        if name not in RETRIEVAL_TOOLS:
            return
        used = getattr(self.shared, "_actual_worker_attempts", {}).get(self.session, 0)
        state = self.snapshot(used=used)
        if state["task_model"]["remaining"] <= self.policy.delivery_reserve:
            raise RetrievalStopped("delivery_reserve_reached")
        if state["retrieval"]["remaining"] == 0:
            raise RetrievalStopped("retrieval_limit_reached")
        if name in state["blocked_tools"]:
            raise RetrievalStopped("retrieval_tool_failure_limit")
