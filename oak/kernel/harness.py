"""Fifth kernel asset H: inference policy, separate from frozen evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Harness:
    version: str = "harness-v1"
    max_steps: int = 10
    context_limit: int = 45
    supplemental_limit: int = 15
    candidate_temperatures: tuple[float, ...] = (0.3, 0.7, 1.0)
    completion_tokens: int = 3072
    review_tokens: int = 3072
    max_format_attempts: int = 3
    max_query_calls: int = 60
    retrieval_mode: str = "legacy"
    answer_mode: str = "candidates"
    refusal_recheck: bool = True
    answer_thinking: bool = True
    requirements_review: bool = False

    def __post_init__(self):
        for name in ("max_steps", "context_limit", "completion_tokens", "review_tokens",
                     "max_format_attempts", "max_query_calls"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.supplemental_limit < 0:
            raise ValueError("supplemental_limit must be nonnegative")
        if not self.candidate_temperatures:
            raise ValueError("At least one candidate required")
        if self.retrieval_mode not in ("legacy", "coverage"):
            raise ValueError("Unknown retrieval mode")
        if self.answer_mode not in ("candidates", "structured"):
            raise ValueError("Unknown answer mode")
        if any(type(v) is not bool for v in (self.refusal_recheck, self.answer_thinking, self.requirements_review)):
            raise ValueError("Harness switches must be boolean")

    def to_dict(self):
        value = asdict(self)
        value["candidate_temperatures"] = list(self.candidate_temperatures)
        return value

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        if "candidate_temperatures" in data:
            data["candidate_temperatures"] = tuple(data["candidate_temperatures"])
        return cls(**data)
