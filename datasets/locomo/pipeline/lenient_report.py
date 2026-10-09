"""Re-evaluate explicitly selected saved answers against versioned HF/local raw inputs."""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from darwinagent.llm.client import LLMClient
from darwinagent.runtime.artifacts import atomic_json

from ..evaluator import LocomoEvaluator
from ..inputs import add_dataset_arguments, resolve_dataset
from ..run import connection


async def main(args):
    root = Path(args.output).resolve()
    data = resolve_dataset(args, root)
    answers = tuple(
        SimpleNamespace(
            question_id=str(row["idx"]),
            answer=row["answer"],
            status="execution_error"
            if row.get("status") == "answer_error"
            else row.get("status", "answered"),
            error=row.get("error"),
        )
        for row in map(json.loads, Path(args.answers).read_text().splitlines())
        if row
    )
    async with LLMClient(connection(root)) as client:
        scores = await LocomoEvaluator(
            client,
            root / "evaluation",
            dataset_path=data / "locomo10_zh.json",
            audited_path=args.audited_reference,
            original_only=not args.audited_reference,
        ).evaluate(SimpleNamespace(case_id=args.case, answers=answers))
    atomic_json(root / "evaluation.json", scores.to_dict())
    print(json.dumps(scores.metrics, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_dataset_arguments(parser)
    parser.add_argument("--case", required=True)
    parser.add_argument("--answers", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--audited-reference")
    asyncio.run(main(parser.parse_args()))
