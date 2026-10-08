"""Run and independently score maintenance records with the installed Python SDK."""

import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import (
    CaseInput,
    Config,
    CorpusBlock,
    EvaluationResult,
    Pipeline,
    QuestionInput,
    RunConfig,
    SourceRef,
    TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient


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
    output = Path("runs/custom-live").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    bundle = load_assets(TASK_ROOT).export(output / "assets")
    spec = TaskSpec.load(TASK_ROOT / "task.yaml", bundle)
    async with asyncio.timeout(1800), LLMClient(config) as client:
        result = await Pipeline(client, output / "generation").run(
            Records().generation_input("device-custom"), spec, RunConfig()
        )
    score = await Score().evaluate(result)
    report = {
        "answers": [
            {
                "question_id": answer.question_id,
                "status": answer.status,
                "answer": answer.answer,
            }
            for answer in result.answers
        ],
        "evaluation": score.to_dict(),
        "output": str(output),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
