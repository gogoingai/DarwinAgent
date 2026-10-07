"""Thin assembly of common scoped execution and independent evaluation."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.kernel import KernelBundle, TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient
from darwinagent.llm.settings import load_legacy_connection as load_connection
from darwinagent.runtime.artifacts import digest
from darwinagent.runtime.execution_cli import add_execution_arguments, selection_from_args

from .adapter import TravelPlannerAdapter
from .evaluator import TravelPlannerEvaluator
from .exports import write

ROOT = Path(__file__).resolve().parents[2]


def evaluator_factory(_client, path):
    return TravelPlannerEvaluator(path)


def _criterion_id():
    official = ROOT / "third_party/TravelPlanner"
    rules = sorted((official / "evaluation").rglob("*.py"))
    references = sorted(p for p in (official / "database").rglob("*") if p.is_file())
    if not rules or not references:
        return None
    sources = [
        ROOT / "datasets/travelplanner/evaluator.py",
        ROOT / "datasets/travelplanner/exports.py",
        *sorted((ROOT / "datasets/travelplanner/pipeline").rglob("*.py")),
        *sorted((ROOT / "datasets/travelplanner/data").glob("*.queries.jsonl")),
        *rules,
        *references,
    ]
    try:
        return digest(
            {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sources
            }
        )
    except OSError:
        return None


evaluator_factory.criterion_id = _criterion_id


async def main(args):
    root = Path(args.output).resolve()
    selection = selection_from_args(args)
    adapter = TravelPlannerAdapter(args.split)
    case = adapter.generation_input(str(args.index))
    if getattr(args, "preview", False):
        from darwinagent.experiments.control import preview

        print(json.dumps(preview(root, [case], selection).to_dict(), ensure_ascii=False))
        return
    task = ROOT / "tasks/travel_planning"
    assets_root = root / "assets"
    bundle = (
        KernelBundle(assets_root)
        if (assets_root / "manifest.json").exists()
        else load_assets(task).export(assets_root)
    )
    spec = TaskSpec.load(task / "task.yaml", bundle)
    connection = load_connection(ROOT, root / "runtime")
    from darwinagent.experiments.stages import selected_stage

    results, scores = await selected_stage(
        "",
        [case],
        spec,
        root=root,
        client_factory=lambda _stage: LLMClient(connection),
        config=RunConfig(),
        evaluator_factory=evaluator_factory,
        execution=selection,
    )
    if results:
        write(results[0], root / "plans.jsonl")
    if scores is not None:
        from darwinagent.runtime.artifacts import atomic_json

        atomic_json(root / "evaluation.json", scores.to_dict())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=["train", "validation"], default="train")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--output", required=True)
    add_execution_arguments(p)
    asyncio.run(main(p.parse_args()))
