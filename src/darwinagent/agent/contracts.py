"""Generic task/episode contracts independent of benchmark inputs and Agent loops."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from darwinagent.contracts import freeze, plain
from darwinagent.runtime.artifacts import digest


@dataclass(frozen=True)
class AgentTask:
    id: str
    instruction: str
    attachments: tuple[Mapping, ...] = ()
    parameters: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if (
            type(self.id) is not str
            or type(self.instruction) is not str
            or not self.id
            or not self.instruction.strip()
        ):
            raise ValueError("Task id and instruction are required")
        if not isinstance(self.parameters, Mapping) or any(
            not isinstance(a, Mapping) for a in self.attachments
        ):
            raise ValueError("Task attachments and parameters must be objects")
        object.__setattr__(self, "attachments", tuple(freeze(a) for a in self.attachments))
        object.__setattr__(self, "parameters", freeze(self.parameters))

    def to_dict(self):
        return {
            "id": self.id,
            "instruction": self.instruction,
            "attachments": plain(self.attachments),
            "parameters": plain(self.parameters),
        }


@dataclass(frozen=True)
class EpisodeResult:
    status: str
    final_response: str = ""
    artifacts: tuple[Mapping, ...] = ()
    events: tuple[Mapping, ...] = ()
    usage: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in {"completed", "rejected", "failed", "unknown", "budget_exhausted"}:
            raise ValueError("Unsupported episode status")
        if type(self.final_response) is not str or not isinstance(self.usage, Mapping):
            raise ValueError("Episode response must be text and usage an object")
        for name in ("artifacts", "events"):
            if any(not isinstance(x, Mapping) for x in getattr(self, name)):
                raise ValueError("Episode artifacts and events must be objects")
            object.__setattr__(self, name, tuple(freeze(x) for x in getattr(self, name)))
        object.__setattr__(self, "usage", freeze(self.usage))

    def to_dict(self):
        return {
            "status": self.status,
            "final_response": self.final_response,
            "artifacts": plain(self.artifacts),
            "events": plain(self.events),
            "usage": plain(self.usage),
        }


class AgentAdapter(Protocol):
    """Adapters own their loop. Ordinary classes also declare identity() -> JSON object.

    Dataclass fields are the default configuration identity; an explicit synchronous
    identity hook can instead name stable operating/external configuration while
    excluding transient loop state. Configurations must contain only JSON values.
    """

    async def run(self, task: AgentTask, context: AgentContext) -> EpisodeResult: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable
    input_contract: Mapping = field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": True}
    )
    output_contract: Mapping = field(default_factory=lambda: {"type": "any"})
    effect: str = "read"
    description: str = ""
    identity: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if not self.name or self.effect not in {"read", "write"} or not callable(self.handler):
            raise ValueError("Invalid registered tool")
        from darwinagent.kernel.spec import contract_errors

        for name in ("input_contract", "output_contract"):
            if contract_errors(getattr(self, name)):
                raise ValueError("Invalid tool contract")
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "identity", freeze(self.identity))

    def declaration(self):
        return {
            "name": self.name,
            "input_contract": plain(self.input_contract),
            "output_contract": plain(self.output_contract),
            "effect": self.effect,
            "description": self.description,
            "identity": plain(self.identity),
        }


@dataclass(frozen=True)
class ExecutionProfile:
    id: str
    capabilities: tuple[str, ...]
    prompt_roles: Mapping = field(
        default_factory=lambda: {"worker": ("instruction", "tools", "view")}
    )
    check_stages: tuple[str, ...] = ("before_action", "after_action", "final_delivery")
    host_checks: tuple[Callable, ...] = ()
    max_model_calls: int = 8
    max_leaf_calls: int = 64

    def __post_init__(self):
        if not self.id or any(
            type(x) is not int or x < 1 for x in (self.max_model_calls, self.max_leaf_calls)
        ):
            raise ValueError("Invalid execution profile")
        if set(self.check_stages) != {"before_action", "after_action", "final_delivery"}:
            raise ValueError("All lifecycle stages are mandatory")
        if not self.prompt_roles or any(
            set(slots) - {"instruction", "tools", "view"} for slots in self.prompt_roles.values()
        ):
            raise ValueError("Unsupported host prompt slot")
        if len(set(self.capabilities)) != len(self.capabilities) or not all(
            callable(c) for c in self.host_checks
        ):
            raise ValueError("Invalid host capabilities or checks")
        object.__setattr__(self, "check_stages", tuple(self.check_stages))
        object.__setattr__(self, "prompt_roles", freeze(self.prompt_roles))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "host_checks", tuple(self.host_checks))

    def declaration(self):
        return {
            "id": self.id,
            "capabilities": list(self.capabilities),
            "prompt_roles": plain(self.prompt_roles),
            "check_stages": list(self.check_stages),
            "max_model_calls": self.max_model_calls,
            "max_leaf_calls": self.max_leaf_calls,
            "host_checks": [self._check_identity(c) for c in self.host_checks],
        }

    @staticmethod
    def _check_identity(check):
        import inspect

        name = f"{check.__module__}.{check.__qualname__}"
        try:
            source = inspect.getsource(check)
        except (OSError, TypeError):
            source = name
        return name + ":" + digest(source)

    @property
    def fingerprint(self):
        return digest(self.declaration())


# Runtime implementation is imported lazily by adapters; this name is only for annotations.

if TYPE_CHECKING:
    from .context import AgentContext
