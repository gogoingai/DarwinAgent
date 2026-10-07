"""Offline shared evaluators fixtures; no test-case dependencies."""

from pathlib import Path

from darwinagent.contracts import (
    EvaluationResult,
)


class StageTaggedEvaluator:
    """Every stage's evaluation carries exactly one diagnostic row tagged with its stage."""

    def __init__(self, stage):
        stage = Path(stage)
        name = stage.parent.name
        self.stage = name if name == "B0" or name.startswith("R") else stage.parent.parent.name

    async def evaluate(self, result, asked=None):
        assert all(a.status == "answered" for a in result.answers)
        diag = ({"question_id": result.answers[0].question_id, "stage_tag": self.stage},)
        return EvaluationResult({"precise": 0 if self.stage == "B0" else 1}, 1, 1, 0, 0, diag)


class FaultyStageEvaluator(StageTaggedEvaluator):
    """R1's evaluation cannot finish scoring: incomplete answers plus an evaluation fault."""

    async def evaluate(self, result, asked=None):
        if self.stage == "R1":
            return EvaluationResult({"precise": 0}, 1, 0, 1, 1)
        return await super().evaluate(result)
