#!/usr/bin/env python3
"""Small public-Runner optimization loop. Replay replaces only model transport.

Live example (connection and key supplied through DARWINAGENT_*):
  PYTHONPATH=src python scripts/smoke_live_loop.py --mode live --model glm-5.3-flash --output runs/live-loop
No extraction or embedding requests are required. Oracle values stay in the evaluator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import networkx as nx

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import CaseInput, CorpusBlock, EvaluationResult, QuestionInput, SourceRef
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.experiments.snapshots import load_frozen_graph
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.assets import KernelAssets
from darwinagent.kernel.registration import load_assets
from darwinagent.kg.graph import node_id, save_graph
from darwinagent.llm.client import LLMClient
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.execution import ActiveBudget, ExecutionSelection
from darwinagent.runtime.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
MODEL = "glm-5.3-flash"


def prepared_graph(snapshot, schema, corpus, embedder_factory=None):
    """Fixed S vocabulary, reusable facts and graph; no model or vector generation."""
    blocks = tuple(corpus.values()) if isinstance(corpus, Mapping) else tuple(corpus)
    return load_frozen_graph(snapshot, blocks)


class FixtureAdapter:
    def __init__(self, cases):
        self.cases = {c.id: c for c in cases}

    def generation_input(self, case_id):
        return self.cases[case_id]


def prepare_fixture(root, mode):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    identity = {"mode": mode, "fixture": "chinese-field-loop-v1", "questions": 4}
    marker = root / "fixture.json"
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise ValueError("Use a separate directory for another fixture or execution mode")
    atomic_json(marker, identity)
    cases, oracle = [], {}
    # Raw input facts are not evaluator labels and are legitimately visible to agents.
    records = (
        ("train-a", (("D-17", "林", "2026-09-01"), ("D-17", "林", "2026-09-02"))),
        ("train-b", (("D-27", "陈", "2026-09-03"), ("D-27", "陈", "2026-09-04"))),
    )
    for cid, rows in records:
        blocks = tuple(
            CorpusBlock(
                SourceRef("maintenance_record", cid, "row-" + str(i)),
                f"设备{serial}于{day}由{person}维护。",
            )
            for i, (serial, person, day) in enumerate(rows)
        )
        questions = tuple(
            QuestionInput(
                "q" + str(i + 1),
                f"{serial}的{'最早' if i == 0 else '最晚'}一条维护记录中，谁在什么时候维护？答案字符串内只输出JSON对象，字段serial、technician、date，date使用YYYY-MM-DD。",
                {"serial": serial},
            )
            for i, (serial, _, _) in enumerate(rows)
        )
        cases.append(CaseInput(cid, blocks, questions))
        for q, (serial, person, day) in zip(questions, rows):
            oracle[(cid, q.id)] = {"serial": serial, "technician": person, "date": day}
        folder = root / "snapshots" / cid
        if not folder.exists():
            folder.mkdir(parents=True)
            graph = nx.MultiDiGraph()
            facts = []
            for block, (serial, person, day) in zip(blocks, rows):
                graph.add_node(
                    node_id("Maintenance", {"serial": serial, "date": day}),
                    etype="Maintenance",
                    __key__=json.dumps({"serial": serial, "date": day}),
                    __sources__=[block.source.id],
                    serial=serial,
                    date=day,
                    technician=person,
                )
                facts.append({"text": block.text, "source_ids": [block.source.id]})
            save_graph(graph, folder / "graph.json")
            graph_digest = digest(json.loads((folder / "graph.json").read_text()))
            (folder / "facts.jsonl").write_text(
                "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in facts)
            )
            (folder / "vector").mkdir()
            (folder / "vector/index.jsonl").write_text("")
            atomic_json(
                folder / "manifest.json",
                {
                    "graph_digest": graph_digest,
                    "facts_digest": digest(facts),
                    "vector_digest": digest([]),
                    "snapshot_digest": digest({"graph": graph_digest, "facts": facts}),
                    "n_facts": len(facts),
                    "n_vector_records": 0,
                },
            )
    seed = root / "seed-assets"
    if not seed.exists():
        assets = load_assets(ROOT / "tasks/device_maintenance")
        assets = tuple(
            replace(a, content="请用简短自然语言回答设备维护人和日期，保留来源证据。")
            if a.role == "answer"
            else replace(
                a,
                content="def run(params):\n    return nodes('Maintenance', {'serial': params['serial']}, limit=1)\n",
                description="基线仅返回首条匹配维护记录。",
            )
            if a.id == "device_lookup"
            else a
            for a in assets.assets
        )
        KernelAssets(assets, origin={"source": identity["fixture"], "seed": True}).export(seed)
    return tuple(cases), oracle, seed, TaskSpec.load(ROOT / "tasks/device_maintenance/task.yaml")


class FieldEvaluator:
    """Independent exact-field evaluator. Diagnostics contain flags, never oracle values."""

    def __init__(self, oracle):
        self.oracle = oracle

    async def evaluate(self, result, asked=None):
        rows, passed, faults = [], 0, 0
        for answer in result.answers:
            try:
                parsed = json.loads(answer.answer)
            except (TypeError, ValueError):
                parsed = None
            expected = self.oracle[(result.case_id, answer.question_id)]
            good = answer.status == "answered" and isinstance(parsed, dict) and parsed == expected
            passed += int(good)
            faults += int(answer.status == "execution_error")
            rows.append(
                {
                    "question_id": answer.question_id,
                    "status": answer.status,
                    "precise": good,
                    "lenient": good,
                    "original": {"precise": good, "lenient": good},
                    "error": answer.error if answer.status == "execution_error" else None,
                }
            )
        total = sum(
            cid == result.case_id and (asked is None or qid in asked) for cid, qid in self.oracle
        )
        return EvaluationResult(
            {"field_exact": passed}, total, len(result.answers) - faults, faults, 0, tuple(rows)
        )


def evaluator_factory(oracle):
    def factory(_client, _path):
        return FieldEvaluator(oracle)

    factory.criterion_id = "synthetic-chinese-exact-fields-v1"
    return factory


class AuditTransport:
    """One shared connection pool and durable request/response provenance across stages."""

    def __init__(self, root, cfg, replay=False, no_change=False, tie_second=False):
        self.root, self.cfg = Path(root), cfg
        self.replay, self.no_change, self.tie_second = replay, no_change, tie_second
        self.client = None if replay else LLMClient(cfg)
        self.concurrency_state = {"peak": 0}
        if self.client is not None:
            try:
                from scripts.smoke_transport_audit import instrument_pool
            except ModuleNotFoundError:
                from smoke_transport_audit import instrument_pool
            self.concurrency_state = instrument_pool(
                self.client, self.root / "transport-concurrency.json"
            )
        self.calls = [
            json.loads(p.read_text()) for p in sorted((self.root / "model-calls").glob("*.json"))
        ]

    def for_stage(self, stage):
        owner = self

        class Lease:
            cfg = owner.cfg

            async def chat(self, **request):
                boundary = getattr(self, "_control_boundary", None)
                if boundary is not None:
                    boundary("model:" + request["role"])
                return await owner.chat(stage, request)

            async def aclose(self):
                pass  # Public Runner closes leases; the single underlying pool lives until shutdown.

            def ledger_summary(self):
                return owner.ledger_summary()

        return Lease()

    def ledger_summary(self):
        return {"total_calls": len(self.calls)} if self.replay else self.client.ledger_summary()

    async def chat(self, stage, request):
        if not self.replay:
            request = {**request, "use_cache": False}
        path = self.root / "model-calls" / (str(len(self.calls)).zfill(6) + ".json")
        record = {
            "stage": stage,
            "requested_model": self.cfg.model_for(request["role"]),
            "state": "submitted",
            "request": request,
        }
        self.calls.append(record)
        atomic_json(path, record)
        try:
            if self.replay:
                response = SimpleNamespace(
                    content=json.dumps(self.recorded_reply(stage, request), ensure_ascii=False),
                    model=MODEL,
                    usage={},
                    cache_hit=False,
                )
            else:
                response = await self.client.chat(**request)
            record.update(
                state="received",
                response={
                    "content": response.content,
                    "model": getattr(response, "model", None),
                    "usage": getattr(response, "usage", {}),
                    "cache_hit": getattr(response, "cache_hit", False),
                    "response_id": getattr(response, "response_id", None),
                },
            )
            atomic_json(path, record)
            return response
        except BaseException as exc:
            record.update(state="unresolved", error=f"{type(exc).__name__}: {exc}")
            atomic_json(path, record)
            raise

    def recorded_reply(self, stage, request):
        role = request["role"]
        payload = json.loads(request["messages"][1]["content"])
        if role == "proposal":
            exchanges = [m for m in request["messages"][2:] if "wiki_reply" in m.get("content", "")]
            if not exchanges:
                return {
                    "action": "query_wiki",
                    "query": {
                        "question": "核对训练输出格式失败、实际作答指引与来源证据，区分猜测。",
                        "view": "raw",
                        "scope": {},
                    },
                }
            if self.no_change:
                return {"action": "no_change", "reason": "回放测试无候选分支", "unresolved": []}
            asset = dict(next(a for a in payload["assets"] if a.get("role") == "answer"))
            asset.pop("fingerprint", None)
            asset.pop("current_ref", None)
            # This deterministic transport is for preflight only, never a live fallback.
            improvement = "fixture_format_v1" if stage == "R1" else "fixture_format_v2"
            asset["content"] += "\n" + improvement + ": 严格遵守问题的字段输出格式并核对日期精度。"
            patches = [
                {
                    "asset": asset,
                    "base_fingerprint": "current:" + asset["id"],
                    "reason": "按训练原件核对通用输出格式与日期精度。",
                    "training_evidence": [q["training_id"] for q in payload["questions"]],
                }
            ]
            if stage == "R2":
                function = dict(next(a for a in payload["assets"] if a["id"] == "device_lookup"))
                function.pop("fingerprint", None)
                function.pop("current_ref", None)
                function["content"] = (
                    "def run(params):\n    return nodes('Maintenance', {'serial': params['serial']}, limit=100)\n"
                )
                patches.append(
                    {
                        "asset": function,
                        "base_fingerprint": "current:device_lookup",
                        "reason": "首条截断未覆盖最晚记录，需真实检索完整匹配证据。",
                        "training_evidence": [q["training_id"] for q in payload["questions"]],
                    }
                )
            return {"action": "submit_patch", "patches": patches}
        if role == "wiki_maintainer":
            return {
                "cause": "训练输出字段格式需核对，这是待验证假设。",
                "action": "对照问题格式和原件提出通用补丁。",
                "training_ids": [],
            }
        if role == "tools":
            return (
                {"action": "ready"}
                if payload["previous_results"]
                else {
                    "action": "call",
                    "asset_id": "device_lookup",
                    "parameters": payload["parameters"],
                }
            )
        if role == "answer":
            evidence = payload["visible_evidence"]
            matching = sorted(
                (r for r in evidence if r["serial"] == payload["parameters"]["serial"]),
                key=lambda r: r["date"],
            )
            row = matching[-1] if "最晚" in payload["question"] else matching[0]
            system = request["messages"][0]["content"]
            value = {k: row[k] for k in ("serial", "technician", "date")}
            v2 = "fixture_format_v2" in system and not self.tie_second
            if "fixture_format_v1" not in system and not v2:
                text = f"{row['technician']}于{row['date']}维护{row['serial']}。"
            else:
                if not v2 and "最晚" in payload["question"]:
                    value["date"] = value["date"].replace("-", "/")
                text = json.dumps(value, ensure_ascii=False)
            return {"status": "answered", "answer": text, "node_ids": [row["node_id"]]}
        if role == "review":
            return {
                "accepted": True,
                "supported": True,
                "subject_correct": True,
                "consistent": True,
                "complete": True,
                "abstention_valid": False,
                "feedback": "回放来源支持维护人和日期",
            }
        raise AssertionError("Prebuilt facts must not invoke role " + role)

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


def build_report(root, mode, summary, transport, error=None):
    root = Path(root)
    rounds, formal = [], []
    workspace = Workspace(root / "workspace")
    for path in sorted(root.glob("R*/decision.json"), key=lambda p: int(p.parent.name[1:])):
        decision = json.loads(path.read_text())
        stage = path.parent.name
        stage_path = path.parent / "stage.json"
        stage_data = json.loads(stage_path.read_text()) if stage_path.exists() else {}
        records = []
        for result_path in sorted((path.parent / "generation").glob("*/result.json")):
            result = json.loads(result_path.read_text())
            records.append(
                {
                    "path": str(result_path),
                    "case_id": result.get("case_id"),
                    "answer_provenance": result.get("answer_provenance", []),
                    "answers": result.get("answers", []),
                }
            )
        candidate = decision.get("candidate_version")
        scores = stage_data.get("scores", {})
        native_models, native_tools, native_complete = 0, 0, True
        for record in records:
            for producer in record["answer_provenance"]:
                journal = producer.get("journal_path")
                steps = list(Path(journal).glob("*.json")) if journal else []
                if not steps:
                    native_complete = False
                for step_path in steps:
                    step = json.loads(step_path.read_text())
                    if step.get("kind") == "tool":
                        native_tools += 1
                    elif step.get("kind") == "model":
                        native_models += 1
                        request = workspace.request(step["request_id"])
                        native_complete &= (
                            step["state"] == request["status"] == "responded"
                            and digest(step["response"]) == step["response_digest"]
                            and workspace.read_json(request["response_ref"]) == step["response"]
                        )
        genuine = bool(
            candidate
            and stage_data.get("status") == "complete"
            and scores.get("completed") == scores.get("total") == 4
            and scores.get("generation_faults") == scores.get("evaluation_faults") == 0
            and native_complete
            and native_models
            and native_tools
            and stage_data.get("asset_version") == candidate
            and records
            and all(
                p.get("asset_version") == candidate for r in records for p in r["answer_provenance"]
            )
            and sum(len(r["answer_provenance"]) for r in records) == 4
            and any(
                c["stage"] == stage and c["request"]["role"] == "answer" for c in transport.calls
            )
        )
        if genuine:
            formal.append(stage)
        rounds.append(
            {
                "stage": stage,
                "outcome": "candidate_evaluated"
                if genuine
                else "round_no_change"
                if decision.get("status") == "no_change"
                else "not_formally_evaluated",
                "decision": decision,
                "stage_record": stage_data,
                "native_model_receipts": native_models,
                "native_tool_results": native_tools,
                "native_steps_complete": native_complete,
                "generation": records,
                "proposal_sessions": [
                    str(p)
                    for p in sorted(
                        (path.parent / "optimization").glob("attempt-*/proposal-call.json")
                    )
                ],
                "candidate_bundle": str(path.parent / "candidate/bundle") if candidate else None,
            }
        )
    database = root / "workspace/workspace.sqlite3"
    try:
        branch = Workspace(database.parent).branch("main") if database.exists() else None
    except KeyError:
        branch = None
    all_calls = [json.loads(p.read_text()) for p in sorted((root / "model-calls").glob("*.json"))]
    completed = bool(
        len(formal) >= 2 and not error and summary and summary.get("status") == "complete"
    )
    report = {
        "mode": mode,
        "requested_model": MODEL,
        "questions": 4,
        "training_cases": 2,
        "concurrency": 2,
        "scope": ["F", "P"],
        "execution_path": "daily_persistent",
        "status": "complete" if completed else "partial",
        "loop_proven": completed,
        "formal_candidate_rounds": formal,
        "candidate_answer_calls": sum(
            c["request"]["role"] == "answer" and c["stage"] in formal for c in all_calls
        ),
        "rounds": rounds,
        "branch": branch,
        "summary": summary,
        "call_count": len(all_calls),
        "ledger": transport.ledger_summary(),
        "http_attempts": 0 if transport.replay else transport.client.http_attempts(),
        "call_records": str(root / "model-calls"),
        "oracle_injected_into_agents": False,
        "http_concurrency_monitor": str(root / "transport-concurrency.json"),
        "transport_peak": transport.concurrency_state["peak"],
        "live_cache_bypassed": not transport.replay,
        "budget_paths": {"requests": str(transport.cfg.request_budget_path)},
        "error": error,
        "claim": "Offline transport preflight only"
        if mode == "replay"
        else "Two candidate rounds executed"
        if len(formal) >= 2
        else "No complete two-round candidate loop proven",
    }
    atomic_json(root / "loop-report.json", report)
    return report


async def execute(
    root,
    *,
    mode="replay",
    model=None,
    rounds=2,
    resume=False,
    active_seconds=1800,
    max_requests=200,
    budget_dir=None,
    client_factory=None,
    no_change=False,
    tie_second=False,
    proposal_attempts=2,
):
    if mode not in ("live", "replay"):
        raise ValueError("Unknown mode")
    if rounds < 2:
        raise ValueError("Loop fixture requires at least two rounds")
    if mode == "live" and model != MODEL:
        raise ValueError(
            "Live requires explicit user-specified --model glm-5.3-flash; no model lookup or switch"
        )
    root = Path(root).resolve()
    cases, oracle, seed, spec = prepare_fixture(root, mode)
    cfg = (
        Config.from_env(work_dir=root / "transport")
        if mode == "live"
        else Config(work_dir=root / "transport")
    )
    cfg.model_strong = cfg.model_middle = cfg.model_fast = MODEL
    cfg.max_concurrency = cfg.fast_max_concurrency = 2
    cfg.max_retries = 1
    cfg.request_timeout_s = 240
    budgets = Path(budget_dir).resolve() if budget_dir else root / "budgets"
    budgets.mkdir(parents=True, exist_ok=True)
    cfg.max_http_requests, cfg.request_budget_path = max_requests, budgets / "http-attempts.json"
    if mode == "live":
        cfg.validate_model()
    transport = AuditTransport(
        root, cfg, replay=mode == "replay", no_change=no_change, tie_second=tie_second
    )
    summary, error = None, None
    try:
        with ActiveBudget(budgets / "active-budget.json", active_seconds) as budget:
            if budget.remaining <= 0:
                raise TimeoutError("Persistent active execution budget exhausted")
            cfg.deadline_monotonic = time.monotonic() + budget.remaining
            runner = ExperimentRunner(
                FixtureAdapter(cases),
                evaluator_factory(oracle),
                cfg,
                RunConfig(
                    concurrency=2,
                    protocol_attempts=2,
                    answer_attempts=2,
                    tool_steps=5,
                    max_tokens=6000,
                    wiki_max_tokens=10000,
                ),
                AdoptionPolicy("field_exact", ()),
                root,
                client_factory=client_factory or transport.for_stage,
                optimization_mode="wiki",
                snapshot_root=root / "snapshots",
                graph_builder=prepared_graph,
                seed_assets=seed,
                proposal_attempts=proposal_attempts,
                round_deadline_s=min(900, active_seconds),
            )
            async with asyncio.timeout(budget.remaining):
                summary = await runner.run(
                    tuple(c.id for c in cases),
                    spec,
                    rounds=rounds,
                    resume=resume,
                    scope=("F", "P"),
                    execution=ExecutionSelection(),
                )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        report = build_report(root, mode, summary, transport, error)
        await transport.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--active-seconds", type=int, default=1800)
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--budget-dir", type=Path)
    parser.add_argument("--proposal-attempts", type=int, default=2)
    args = parser.parse_args()
    if args.mode == "live" and args.model != MODEL:
        parser.error(
            "Live requires --model glm-5.3-flash and supplied DARWINAGENT connection variables"
        )
    report = asyncio.run(
        execute(
            args.output,
            mode=args.mode,
            model=args.model,
            rounds=args.rounds,
            resume=args.resume,
            active_seconds=args.active_seconds,
            max_requests=args.max_requests,
            budget_dir=args.budget_dir,
            proposal_attempts=args.proposal_attempts,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["loop_proven"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
