"""Three independently prepared live cases with deterministic JSON-field evaluation.

Model and endpoint must be supplied explicitly; never discovers models or embeddings.
Evaluation references stay in this controller, outside Pipeline and Wiki payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import networkx as nx
import smoke_intervention as base

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from darwinagent.engine import Pipeline
from darwinagent.kg.graph import node_id, save_graph
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.execution import ActiveBudget, ExecutionSelection
from darwinagent.runtime.steps import StepJournal, UnknownRequest
from darwinagent.runtime.workspace import Workspace


def fixtures(root, definitions=None):
    definitions = (
        definitions
        if definitions is not None
        else [
            (
                "subject-date",
                [("D-31", "2026-01-02", "林"), ("D-32", "2026-03-09", "赵")],
                [
                    (
                        "q1",
                        "D-32由谁在何时维护？只返回technician和date字段。",
                        "D-32",
                        {"technician": "赵", "date": "2026-03-09"},
                    ),
                    (
                        "q2",
                        "D-31由谁在何时维护？只返回technician和date字段。",
                        "D-31",
                        {"technician": "林", "date": "2026-01-02"},
                    ),
                ],
            ),
            (
                "historical-refusal",
                [("D-51", "2026-07-02", "王"), ("D-51", "2026-07-03", "李")],
                [
                    (
                        "q1",
                        "D-51在2026-07-03由谁维护？只返回该事件technician和date。",
                        "D-51",
                        {"technician": "李", "date": "2026-07-03"},
                    ),
                    ("q2", "D-59由谁在何时维护？如无记录，应abstained。", "D-59", None),
                ],
            ),
            (
                "paging-tail",
                [("D-71", str(date(2026, 1, 1) + timedelta(days=i)), "陈") for i in range(75)],
                [
                    (
                        "q1",
                        "D-71共有多少条记录？必须用f_page_facts，limit=25，从offset=0逐页读取到结束，只返回count字段。",
                        "D-71",
                        {"count": 75},
                    ),
                    (
                        "q2",
                        "D-71最后一条记录的技术员和日期？使用f_page_facts逐页核对，只返回technician和date。",
                        "D-71",
                        {"technician": "陈", "date": "2026-03-16"},
                    ),
                ],
            ),
        ]
    )
    output = []
    for case_id, records, questions in definitions:
        folder = root / case_id
        folder.mkdir(parents=True, exist_ok=True)
        _, _, bundle, task = base.prepare_fixture(folder / "asset-template", "live")
        block = CorpusBlock(
            SourceRef("maintenance_record", case_id, "all-records"),
            "\n".join(f"设备{s}于{d}由{t}维护。" for s, d, t in records),
        )
        case = CaseInput(
            case_id,
            (block,),
            tuple(
                QuestionInput(qid, text, {"serial": serial}) for qid, text, serial, _ in questions
            ),
        )
        snapshot = folder / "prepared"
        snapshot.mkdir(exist_ok=True)
        graph = nx.MultiDiGraph()
        facts = []
        for serial, day, technician in records:
            graph.add_node(
                node_id("Maintenance", {"serial": serial, "date": day}),
                etype="Maintenance",
                __key__=json.dumps({"serial": serial, "date": day}),
                __sources__=[block.source.id],
                serial=serial,
                date=day,
                technician=technician,
            )
            facts.append(
                {
                    "id": str(len(facts)),
                    "text": f"设备{serial}于{day}由{technician}维护。",
                    "source_ids": [block.source.id],
                }
            )
        save_graph(graph, snapshot / "graph.json")
        payload = json.loads((snapshot / "graph.json").read_text())
        (snapshot / "facts.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in facts)
        )
        (snapshot / "vector").mkdir(exist_ok=True)
        (snapshot / "vector/index.jsonl").write_text("")
        atomic_json(
            snapshot / "manifest.json",
            {
                "graph_digest": digest(payload),
                "facts_digest": digest(facts),
                "vector_digest": digest([]),
                "snapshot_digest": digest(payload),
                "n_facts": len(facts),
                "n_vector_records": 0,
            },
        )
        task = replace(
            task,
            answer_format="json",
            answer_contract={
                "type": "object",
                "properties": {
                    "technician": {"type": "string"},
                    "date": {"type": "string"},
                    "count": {"type": "integer"},
                    "technicians": {"type": "array", "items": {"type": "string"}},
                },
            },
        )
        output.append((case, snapshot, bundle, task, {q[0]: q[3] for q in questions}))
    return output


def evaluate(result, expected):
    rows = []
    for answer in result.answers:
        reference = expected[answer.question_id]
        if reference is None:
            value = None
            passed = answer.status == "abstained"
        else:
            try:
                value = json.loads(answer.answer)
            except (ValueError, TypeError):
                value = None
            passed = (
                answer.status == "answered"
                and isinstance(value, dict)
                and all(
                    (
                        isinstance(value.get(k), list)
                        and all(isinstance(item, str) for item in value[k])
                        and len(value[k]) == len(v)
                        and sorted(value[k]) == sorted(v)
                    )
                    if isinstance(v, list)
                    else type(value.get(k)) is type(v) and value.get(k) == v
                    for k, v in reference.items()
                )
            )
        rows.append(
            {
                "question_id": answer.question_id,
                "status": answer.status,
                "actual": value,
                "expected": reference,
                "passed": passed,
                "answer": answer.answer,
                "node_ids": list(answer.node_ids),
            }
        )
    return {"total": len(expected), "passed": sum(r["passed"] for r in rows), "rows": rows}


async def live(root, model, active_seconds=1200, max_requests=70, *, definitions=None):
    if not model.strip():
        raise ValueError("Explicit user-selected model required")
    cases = fixtures(root, definitions)
    cfg = Config.from_env(work_dir=root / "transport")
    cfg.model_strong = cfg.model_middle = cfg.model_fast = model
    cfg.max_concurrency = cfg.fast_max_concurrency = 1
    cfg.request_timeout_s = 240
    cfg.max_http_requests = max_requests
    cfg.request_budget_path = root / "http-attempts.json"
    cfg.validate_model()
    workspace = Workspace(root / "workspace")
    run_config = RunConfig(concurrency=1, protocol_attempts=3, answer_attempts=3, tool_steps=10)
    report = {
        "requested_model": model,
        "concurrency": 1,
        "status": "pending",
        "cases": [],
        "limits": {"http_attempts": max_requests, "active_seconds": active_seconds},
    }
    client = None
    try:
        with ActiveBudget(root / "active-budget.json", active_seconds) as budget:
            if not budget.remaining:
                raise ValueError("Active budget exhausted; progress retained")
            cfg.deadline_monotonic = time.monotonic() + budget.remaining
            client = base.LLMClient(cfg)
            async with asyncio.timeout(budget.remaining):
                for index, (case, snapshot, bundle, task, expected) in enumerate(cases):
                    pipeline = Pipeline(
                        client,
                        root / "generation",
                        frozen_snapshot=snapshot,
                        graph_builder=base.prepared_graph,
                        embedder_factory=base.NoEmbedding,
                        workspace=workspace,
                    )
                    if index == 0 and not (root / "interrupt-injection.json").exists():
                        original = StepJournal.respond
                        injected = []

                        def crash_after_receipt(
                            journal, path, response, original=original, injected=injected
                        ):
                            original(journal, path, response)
                            if not injected and response.get("role") == "tools":
                                injected.append(str(path))
                                atomic_json(
                                    root / "interrupt-injection.json",
                                    {
                                        "saved_step": str(path),
                                        "request_id": json.loads(path.read_text())["request_id"],
                                        "http_attempts": client.http_attempts(),
                                    },
                                )
                                raise RuntimeError(
                                    "Fault injection after durable model response commit"
                                )

                        StepJournal.respond = crash_after_receipt
                        try:
                            await pipeline.run(
                                case, task, run_config, execution=ExecutionSelection()
                            )
                        except UnknownRequest:
                            pass
                        finally:
                            StepJournal.respond = original
                        if not injected:
                            raise AssertionError("Receipt interruption fixture did not execute")
                    try:
                        result = await pipeline.run(
                            case, task, run_config, execution=ExecutionSelection()
                        )
                    except UnknownRequest as exc:
                        report.setdefault("unresolved", []).append(
                            {"case_id": case.id, "error": str(exc)}
                        )
                        atomic_json(root / "extended-report.json", report)
                        continue
                    evaluation = evaluate(result, expected)
                    start = client.http_attempts()
                    await pipeline.run(
                        case,
                        task,
                        replace(run_config, concurrency=2),
                        execution=ExecutionSelection(),
                    )
                    reuse = client.http_attempts() - start
                    if reuse:
                        raise AssertionError(
                            "Completed answers were regenerated after control change"
                        )
                    if index == 0:
                        try:
                            workspace.branch("manual-fork")
                        except KeyError:
                            workspace.create_branch("manual-fork", parent="main")
                        await pipeline.run(
                            case,
                            task,
                            run_config,
                            execution=ExecutionSelection(mode="fork", branch="manual-fork"),
                        )
                        if client.http_attempts() != start:
                            raise AssertionError("Fork called models for existing success")
                        injected = json.loads((root / "interrupt-injection.json").read_text())
                        saved = json.loads(Path(injected["saved_step"]).read_text())
                        if (
                            saved["request_id"] != injected["request_id"]
                            or workspace.request(saved["request_id"])["status"] != "responded"
                        ):
                            raise AssertionError("Resume discarded the original saved response")
                        report["receipt_resume"] = {
                            "same_request_id": True,
                            "saved_response_reused": True,
                        }
                    report["cases"].append(
                        {
                            "case_id": case.id,
                            "evaluation": evaluation,
                            "reuse_new_http_attempts": reuse,
                            "answer_provenance": list(result.to_dict()["answer_provenance"]),
                        }
                    )
                    atomic_json(root / "extended-report.json", report)
                    print(
                        json.dumps(
                            {
                                "case_id": case.id,
                                "passed": evaluation["passed"],
                                "total": evaluation["total"],
                                "http_attempts": client.http_attempts(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                report["status"] = (
                    "pending"
                    if report.get("unresolved")
                    else "complete"
                    if all(
                        c["evaluation"]["passed"] == c["evaluation"]["total"]
                        for c in report["cases"]
                    )
                    else "failed"
                )
    except Exception as exc:
        report.update(status="pending", error=f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            report["http_attempts"] = client.http_attempts()
            report["ledger"] = client.ledger_summary()
            await client.aclose()
        atomic_json(root / "extended-report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--active-seconds", type=int, default=1200)
    parser.add_argument("--max-requests", type=int, default=70)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(live(args.output, args.model, args.active_seconds, args.max_requests)),
            ensure_ascii=False,
            indent=2,
        )
    )
