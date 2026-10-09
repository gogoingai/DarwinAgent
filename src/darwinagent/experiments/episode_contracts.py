"""Host-owned task/evaluator contracts and independent episode selection metrics."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Protocol

from darwinagent.agent import AgentTask, EpisodeResult
from darwinagent.contracts import freeze, plain


class AgentDataset(Protocol):
    """Only public generation inputs. Private references stay inside the evaluator."""

    def task(self, task_id: str) -> AgentTask: ...

    def identity(self) -> dict: ...


@dataclass(frozen=True)
class EpisodeEvaluation:
    success: float
    content: float
    healthy: bool = True
    public_feedback: tuple[str, ...] = ()
    correction_turns: int | None = None

    def __post_init__(self):
        if (
            any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (self.success, self.content)
            )
            or type(self.healthy) is not bool
        ):
            raise ValueError("Invalid episode quality metrics")
        if not 0 <= self.success <= 1 or self.content < 0:
            raise ValueError("Success must be in [0, 1] and content must be nonnegative")
        if self.correction_turns is not None and (
            type(self.correction_turns) is not int or self.correction_turns < 0
        ):
            raise ValueError("Correction turns must be measured or unknown")
        if isinstance(self.public_feedback, str) or not all(
            isinstance(v, str) for v in self.public_feedback
        ):
            raise ValueError("Public feedback must contain strings")
        object.__setattr__(self, "public_feedback", tuple(self.public_feedback))

    def to_dict(self):
        return plain(freeze(asdict(self)))


class EpisodeEvaluator(Protocol):
    """Declare criterion/reference identities; never return private gold in feedback."""

    def declaration(self) -> dict: ...

    async def evaluate(self, task: AgentTask, episode: EpisodeResult) -> EpisodeEvaluation: ...


@dataclass(frozen=True)
class AgentExperimentSpec:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    admission: tuple[str, ...]
    test: tuple[str, ...] = ()
    rounds: int = 1
    max_episode_runs: int = 100
    proposal_call_limit: int = 4

    def __post_init__(self):
        for name in ("train", "validation", "admission", "test"):
            if isinstance(getattr(self, name), str):
                raise ValueError("Episode splits must contain task ids, not a single string")
            ids = tuple(getattr(self, name))
            if (not ids and name != "test") or any(type(x) is not str or not x for x in ids):
                raise ValueError("Admission, training and validation splits must be nonempty")
            if len(ids) != len(set(ids)):
                raise ValueError("Episode split ids must be unique")
            object.__setattr__(self, name, ids)
        if (
            set(self.train) & set(self.validation)
            or set(self.train) & set(self.test)
            or set(self.validation) & set(self.test)
            or set(self.admission) & (set(self.validation) | set(self.test))
        ):
            raise ValueError("Holdout splits must be disjoint from training and admission")
        for name in ("rounds", "max_episode_runs", "proposal_call_limit"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "rounds" else 1):
                raise ValueError("Invalid finite experiment budget: " + name)

    def declaration(self):
        return plain(freeze(asdict(self)))


def episode_metrics(episode, evaluation):
    """Use leaf receipts and worker usage, never proposer/judge token consumption."""
    models = [e for e in episode.events if e.get("kind") == "model"]
    tokens = 0
    for event in models:
        usage = event.get("usage", {})
        parts = [usage.get(k) for k in ("prompt_tokens", "completion_tokens")]
        if any(type(v) is not int or v < 0 for v in parts):
            tokens = None
            break
        tokens += sum(parts)
    if episode.usage.get("model_calls") != len(models):
        tokens = None
    leaves = episode.usage.get("leaf_calls")
    if type(leaves) is not int or leaves < 0:
        leaves = None
    return {
        "success": evaluation.success,
        "content": evaluation.content,
        "healthy": evaluation.healthy and episode.status == "completed",
        "correction_turns": evaluation.correction_turns,
        "leaf_calls": leaves,
        "execution_tokens": tokens,
    }


def select_episode_candidate(baseline, candidate):
    """Complete paired quality first, then lexicographic measured efficiency."""
    if not baseline or set(baseline) != set(candidate):
        return {"accepted": False, "reason": "incomplete_pairs"}
    if any(not row["healthy"] for rows in (baseline, candidate) for row in rows.values()):
        return {"accepted": False, "reason": "unhealthy_evaluation"}
    totals = [
        {key: sum(r[key] for r in rows.values()) for key in ("success", "content")}
        for rows in (baseline, candidate)
    ]
    before, after = totals
    if any(after[k] < before[k] for k in before):
        return {"accepted": False, "reason": "quality_regression", "quality": totals}
    if any(after[k] > before[k] for k in before):
        return {"accepted": True, "reason": "quality_improvement", "quality": totals}
    # Missing evidence anywhere in the efficiency vector preserves K0.
    keys = ("correction_turns", "leaf_calls", "execution_tokens")
    if any(r.get(k) is None for rows in (baseline, candidate) for r in rows.values() for k in keys):
        return {"accepted": False, "reason": "unknown_efficiency", "quality": totals}
    efficiency = [
        tuple(sum(r[k] for r in rows.values()) for k in keys) for rows in (baseline, candidate)
    ]
    return {
        "accepted": efficiency[1] < efficiency[0],
        "reason": "efficiency_improvement"
        if efficiency[1] < efficiency[0]
        else "efficiency_tie_or_regression",
        "quality": totals,
        "efficiency": [list(v) for v in efficiency],
    }
