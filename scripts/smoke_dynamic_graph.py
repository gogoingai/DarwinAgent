"""Bounded live LoCoMo graph/Wiki acceptance with an explicitly supplied model and HF input."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import CaseInput
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.kernel import TaskSpec
from darwinagent.runtime.artifacts import atomic_json, digest
from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.evaluator import LOCK_PATH, LocomoEvaluator
from datasets.locomo.inputs import add_dataset_arguments, prepare_memory, resolve_dataset
from datasets.locomo.llm_graph import LLMSnapshotGraphBuilder, llm_structure_sample
from scripts.smoke_live_loop import AuditTransport, FixtureAdapter

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    root = Path(args.output).resolve()
    data = resolve_dataset(args, root)
    full = LocomoAdapter(data / "locomo10_zh.json").generation_input("conv-26")
    # Fixed session prefix independent of question/gold content. Original IDs retained.
    corpus = tuple(b for b in full.corpus if int(b.source.location.split(":")[0][1:]) <= 3)
    if args.messages:
        corpus = corpus[: args.messages]
    question_ids = (
        set(args.question_ids.split(",")) if args.question_ids else set(str(i) for i in range(8))
    )
    case = CaseInput(full.id, corpus, tuple(q for q in full.questions if q.id in question_ids))
    cfg = Config(
        api_base_url=args.base_url,
        api_key=Path(args.key_file).read_text().strip(),
        model_strong=args.model,
        model_middle=args.model,
        model_fast=args.model,
        reasoning_effort="low",
        thinking_type="adaptive",
        max_concurrency=args.concurrency,
        max_retries=1,
        request_timeout_s=180,
        max_http_requests=500,
        deadline_monotonic=time.monotonic() + 3600,
        request_budget_path=root / "http-attempts.json",
        work_dir=root / "runtime",
    )
    cfg.role_tiers["locomo_judge"] = "strong"
    cfg.empty_response_passthrough_roles.add("locomo_judge")
    transport = AuditTransport(root, cfg)
    config = RunConfig(
        concurrency=args.concurrency,
        extraction_batch_chars=2000,
        extraction_max_tokens=10000,
        max_tokens=14000,
        wiki_max_tokens=14000,
        protocol_attempts=3,
        answer_attempts=2,
        tool_steps=6,
        function_timeout_s=15,
    )
    atomic_json(
        root / "scope.json",
        {
            "model": args.model,
            "base_url": args.base_url,
            "reasoning_effort": "low",
            "thinking": {"type": "adaptive"},
            "concurrency": args.concurrency,
            "case": case.id,
            "sessions": sorted({int(b.source.location.split(":")[0][1:]) for b in corpus}),
            "messages": len(corpus),
            "train_question_ids": [q.id for q in case.questions],
            "rounds": args.rounds,
            "vector_mode": "none",
            "scope": ["S", "F", "C", "P"],
            "claim": "bounded mechanism acceptance; no full LoCoMo or vector-performance claim",
        },
    )
    memory_root = Path(args.memory_root).resolve() if args.memory_root else root / "memory"
    outcome, error = None, None
    try:
        manifest = await prepare_memory(
            case,
            memory_root,
            transport.for_stage("facts"),
            replace(config, extraction_batch_chars=800),
        )
        print(
            json.dumps({"stage": "facts", "facts": manifest["n_facts"], "messages": len(corpus)}),
            flush=True,
        )
        if args.phase == "prepare":
            return
        lock = json.loads(LOCK_PATH.read_text())
        run_lock = root / "evaluation-lock.json"
        if run_lock.exists() and json.loads(run_lock.read_text()) != lock:
            raise ValueError("Evaluation lock changed")
        atomic_json(run_lock, lock)

        def evaluator(client, path):
            return LocomoEvaluator(
                client,
                path,
                dataset_path=data / "locomo10_zh.json",
                original_only=True,
                lock_path=run_lock,
                concurrency=args.concurrency,
                dataset_hashes=json.loads((root / "dataset-source.json").read_text())["files"],
            )

        evaluator.criterion_id = digest(lock)
        task = replace(
            TaskSpec.load(ROOT / "tasks/conversation_memory/task.yaml"),
            retrieval_floor={"traversal": True},
        )
        runner = ExperimentRunner(
            FixtureAdapter((case,)),
            evaluator,
            cfg,
            config,
            AdoptionPolicy("original_precise", ("original_lenient",)),
            root / "train",
            frozen_files=[
                *(
                    ROOT / "datasets/locomo" / name
                    for name in (
                        "adapter.py",
                        "evaluator.py",
                        "exports.py",
                        "run.py",
                        "inputs.py",
                        "graph_rules.py",
                        "llm_graph.py",
                        "pipeline",
                    )
                ),
                ROOT / "tasks/conversation_memory",
                run_lock,
                root / "dataset-source.json",
            ],
            client_factory=transport.for_stage,
            bootstrap_context=llm_structure_sample(memory_root / case.id, max_facts=12),
            snapshot_root=memory_root,
            graph_builder=LLMSnapshotGraphBuilder(
                Path(args.graph_cache_root).resolve()
                if args.graph_cache_root
                else root / "graph-cache"
            ),
            optimization_mode="wiki",
            wiki_call_limit=12,
            proposal_attempts=3,
            round_deadline_s=600,
            seed_assets=Path(args.seed_assets).resolve() if args.seed_assets else None,
        )
        outcome = await runner.run(
            case.id, task, rounds=args.rounds, scope=("S", "F", "C", "P"), resume=args.resume
        )
        print(json.dumps({"stage": "complete", "status": outcome["status"]}), flush=True)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        atomic_json(
            root / "live-summary.json",
            {
                "outcome": outcome,
                "error": error,
                "ledger": transport.ledger_summary(),
                "concurrency": transport.concurrency_state,
                "received": sum(c["state"] == "received" for c in transport.calls),
                "unresolved": sum(c["state"] != "received" for c in transport.calls),
            },
        )
        await transport.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_dataset_arguments(parser)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--phase", choices=("prepare", "all"), default="all")
    parser.add_argument("--messages", type=int, help="显式限制固定原文前缀的消息数")
    parser.add_argument("--question-ids", help="显式选择原始训练题号")
    parser.add_argument("--graph-cache-root", help="显式复用同输入身份的构图缓存")
    parser.add_argument("--memory-root", help="显式复用冻结事实包")
    parser.add_argument("--seed-assets", help="显式使用中性初始资产；准入和评分照常执行")
    parser.add_argument("--resume", action="store_true")
    asyncio.run(main(parser.parse_args()))
