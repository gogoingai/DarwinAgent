"""Improve user-supplied maintenance records with an independently scored experiment."""

import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import (
    AdoptionPolicy,
    CaseInput,
    Config,
    CorpusBlock,
    EvaluationResult,
    ExperimentRunner,
    QuestionInput,
    RunConfig,
    SourceRef,
    TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets


class Records:
    def generation_input(self, case_id):
        return CaseInput(
            case_id,
            (
                CorpusBlock(
                    SourceRef("maintenance_record", case_id, "row-1"),
                    "Device D-17 was maintained by Lin on 2026-09-01.",
                ),
            ),
            (QuestionInput("q1", "Who maintained D-17 and when?", {"serial": "D-17"}),),
        )


class Score:
    async def evaluate(self, result):
        correct = sum(
            answer.status == "answered" and "Lin" in answer.answer and "2026-09-01" in answer.answer
            for answer in result.answers
        )
        faults = sum(answer.status == "execution_error" for answer in result.answers)
        return EvaluationResult(
            {"correct": correct},
            len(result.answers),
            len(result.answers) - faults,
            faults,
            0,
        )


async def main():
    load_dotenv(Path(".env"))
    output = Path("runs/custom-experiment").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    seed = output / "seed"
    load_assets(TASK_ROOT).export(seed)

    def evaluator_factory(_client, _path):
        return Score()

    evaluator_factory.criterion_id = "maintenance-fields-v1"
    runner = ExperimentRunner(
        Records(),
        evaluator_factory,
        config,
        RunConfig(protocol_attempts=2, answer_attempts=2, tool_steps=3, max_tokens=1800),
        AdoptionPolicy("correct", ()),
        output,
        optimization_mode="wiki",
        wiki_call_limit=6,
        proposal_attempts=2,
        seed_assets=seed,
    )
    async with asyncio.timeout(1800):
        summary = await runner.run(
            "device-custom",
            TaskSpec.load(TASK_ROOT / "task.yaml"),
            rounds=2,
            scope=("P",),
        )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    asyncio.run(main())
