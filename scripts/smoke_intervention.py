#!/usr/bin/env python3
"""Five prepared-graph questions and dynamic Wiki dialogue, explicit offline replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import networkx as nx

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import CaseInput, CorpusBlock, EvaluationResult, QuestionInput, SourceRef
from darwinagent.engine.pipeline import Pipeline
from darwinagent.experiments.feedback import _wiki_training_evidence
from darwinagent.experiments.proposal import ProposalGenerator
from darwinagent.experiments.proposal_session import ProposalSession
from darwinagent.experiments.snapshots import load_frozen_graph
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.experiments.wiki_evidence import asset_evidence
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.registration import load_assets
from darwinagent.kg.graph import node_id, save_graph
from darwinagent.llm.client import LLMClient
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.execution import ActiveBudget, ExecutionSelection
from darwinagent.runtime.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def prepared_graph(snapshot, schema, corpus, embedder_factory):
    return load_frozen_graph(snapshot, tuple(corpus.values()))


class NoEmbedding:
    model, base_url, dim = "offline-fixture", "offline-fixture", 1

    def embed(self, text):
        raise AssertionError("Prepared graph smoke must not request embeddings")


class DialogueClient(RecordedClient):
    async def chat(self, **request):
        if request["role"] == "wiki_maintainer":
            payload = json.loads(request["messages"][1]["content"])
            ref = payload["evidence"]["ref"]
            self.replies.setdefault("wiki_maintainer", __import__("collections").deque()).append(
                {
                    "facts": [{"ref": ref, "text": "原件分页offset依次0、50、100，总计130条。"}],
                    "uncertainty": [{"ref": ref, "text": "这是离线回放，未证明真实模型推理质量。"}],
                }
            )
        return await super().chat(**request)

    async def aclose(self):
        pass


class ExactFixtureEvaluator:
    """Independent controller-side answer expectations; never injected into Wiki."""

    async def evaluate(self, result):
        expected = {
            "q1": "林",
            "q2": "2026-09-01",
            "q3": "130条",
            "q4": "D-17由林、D-18由赵维护",
            "q5": "信息不足",
        }
        passed = sum(
            a.answer == expected[a.question_id]
            and a.status == ("abstained" if a.question_id == "q5" else "answered")
            for a in result.answers
        )
        return EvaluationResult(
            {"fixture_exact": passed},
            5,
            len(result.answers),
            sum(a.status == "execution_error" for a in result.answers),
            0,
        )


def reviews():
    return [
        {
            "accepted": True,
            "supported": True,
            "subject_correct": True,
            "consistent": True,
            "complete": True,
            "abstention_valid": False,
            "feedback": "fixture supported",
        }
    ] * 4 + [
        {
            "accepted": True,
            "supported": False,
            "subject_correct": True,
            "consistent": True,
            "complete": True,
            "abstention_valid": True,
            "feedback": "no source",
        }
    ]


def prepare_fixture(root, mode):
    root.mkdir(parents=True, exist_ok=True)
    mode_path = root / "smoke-mode.json"
    if mode_path.exists() and json.loads(mode_path.read_text())["mode"] != mode:
        raise ValueError("Live and replay require separate output directories")
    if (root / "smoke-report.json").exists():
        previous_mode = json.loads((root / "smoke-report.json").read_text())["mode"]
        if previous_mode != ("offline_replay" if mode == "replay" else "live"):
            raise ValueError(
                "Existing results have another execution source; select a separate output directory"
            )
    atomic_json(mode_path, {"mode": mode})
    source = CorpusBlock(
        SourceRef("maintenance_record", "smoke-case", "prepared-records"),
        "\n".join(f"设备D-17于{date(2026, 9, 1) + timedelta(days=i)}由林维护。" for i in range(130))
        + "\n设备D-18于2027-01-09由赵维护。",
    )
    case = CaseInput(
        "smoke-case",
        (source,),
        tuple(
            QuestionInput(qid, text, {"serial": serial})
            for qid, text, serial in [
                ("q1", "谁维护D-17？", "D-17"),
                ("q2", "D-17首条维护日期？", "D-17"),
                ("q3", "D-17共有多少条维护记录？请跨页检索。", "D-17"),
                ("q4", "D-17和D-18分别由谁维护？", "D-17"),
                ("q5", "谁维护D-999？", "D-999"),
            ]
        ),
    )
    snapshot = root / "prepared"
    if not snapshot.exists():
        snapshot.mkdir()
        graph = nx.MultiDiGraph()
        for i in range(131):
            serial, technician = ("D-17", "林") if i < 130 else ("D-18", "赵")
            day = str(date(2026, 9, 1) + timedelta(days=i))
            graph.add_node(
                node_id("Maintenance", {"serial": serial, "date": day}),
                etype="Maintenance",
                __key__=json.dumps({"serial": serial, "date": day}),
                __sources__=[source.source.id],
                serial=serial,
                date=day,
                technician=technician,
            )
        save_graph(graph, snapshot / "graph.json")
        payload = json.loads((snapshot / "graph.json").read_text())
        facts = [
            {
                "id": str(i),
                "text": f"设备{'D-17' if i < 130 else 'D-18'}于{date(2026, 9, 1) + timedelta(days=i)}由{'林' if i < 130 else '赵'}维护。",
                "source_ids": [source.source.id],
            }
            for i in range(131)
        ]
        (snapshot / "facts.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in facts), encoding="utf-8"
        )
        (snapshot / "vector").mkdir()
        (snapshot / "vector/index.jsonl").write_text("", encoding="utf-8")
        atomic_json(
            snapshot / "manifest.json",
            {
                "graph_digest": digest(payload),
                "facts_digest": digest(facts),
                "vector_digest": digest([]),
                "snapshot_digest": digest(payload),
                "n_facts": 131,
                "n_vector_records": 0,
            },
        )
    bundle_path = root / "assets"
    if not bundle_path.exists():
        assets = load_assets(ROOT / "tasks/device_maintenance")
        page = Asset(
            "f_page_facts",
            "F",
            "def run(params):\n    records = nodes('Maintenance', {'serial': params['serial']}, limit=5000)\n    offset = params['offset']\n    limit = params['limit']\n    rows = records[offset:offset + limit]\n    return {'rows': rows, 'next_offset': offset + len(rows), 'more_remain': offset + len(rows) < len(records), 'matched_count': len(records)}\n",
            input_contract={
                "type": "object",
                "properties": {
                    "serial": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["serial", "offset", "limit"],
            },
            output_contract={"type": "any"},
            schema_dependencies=("schema",),
            trial_inputs=({"serial": "D-17", "offset": 0, "limit": 50},),
        )
        KernelAssets(
            (*assets.assets, page),
            origin={"source": "prepared-smoke-fixture", "execution_mode": mode},
        ).export(bundle_path)
    from darwinagent.kernel.assets import KernelBundle

    bundle = KernelBundle(bundle_path)
    spec = TaskSpec.load(ROOT / "tasks/device_maintenance/task.yaml", bundle)
    return case, snapshot, bundle, spec


def recorded_generation_client(case):
    tools = []
    for q in case.questions:
        if q.id == "q3":
            tools.extend(
                {
                    "action": "call",
                    "asset_id": "f_page_facts",
                    "parameters": {"serial": "D-17", "offset": offset, "limit": 50},
                }
                for offset in (0, 50, 100)
            )
        else:
            tools.append(
                {"action": "call", "asset_id": "device_lookup", "parameters": dict(q.parameters)}
            )
            if q.id == "q4":
                tools.append(
                    {
                        "action": "call",
                        "asset_id": "device_lookup",
                        "parameters": {"serial": "D-18"},
                    }
                )
        tools.append({"action": "ready"})
    client = RecordedClient(
        {
            "tools": tools,
            "answer": [
                {"status": "answered", "answer": text, "node_ids": ids}
                for text, ids in [
                    ("林", ["n000000"]),
                    ("2026-09-01", ["n000000"]),
                    ("130条", ["n000000", "n000129"]),
                    ("D-17由林、D-18由赵维护", ["n000000", "n000130"]),
                ]
            ]
            + [{"status": "abstained", "answer": "信息不足", "node_ids": []}],
            "review": reviews(),
        }
    )
    return client


async def replay(root):
    config = RunConfig(concurrency=1, protocol_attempts=1, answer_attempts=1, tool_steps=5)
    case, snapshot, bundle, spec = prepare_fixture(root, "replay")
    client = recorded_generation_client(case)
    pipeline = Pipeline(
        client,
        root / "generation",
        frozen_snapshot=snapshot,
        graph_builder=prepared_graph,
        embedder_factory=NoEmbedding,
        workspace=Workspace(root / "workspace"),
    )
    result = await pipeline.run(case, spec, config, execution=ExecutionSelection())
    assert [a.status for a in result.answers] == ["answered"] * 4 + ["abstained"], result.to_dict()
    scores = await ExactFixtureEvaluator().evaluate(result)
    assert scores.metrics["fixture_exact"] == 5
    first_calls = len(client.calls)
    await pipeline.run(case, spec, replace(config, concurrency=2), execution=ExecutionSelection())
    assert len(client.calls) == first_calls, "Successful answers were repeated"
    dialogue = DialogueClient(
        {
            "proposal": [
                {
                    "action": "query_wiki",
                    "query": {
                        "question": "检查q3跨页是否推进，并重新归纳原件",
                        "scope": {"case_ids": [case.id], "question_ids": ["q3"]},
                        "view": "regroup",
                    },
                },
                {"action": "no_change", "reason": "分页原件支持推进，保留真实模型验证待办"},
            ]
        }
    )
    wiki = WikiMaintainer(root, "offline-five-question", lambda _: dialogue, config)
    await wiki.record(
        "smoke", "formal", {**asset_evidence(bundle), **_wiki_training_evidence((case,), (result,))}
    )
    patches = await ProposalGenerator().propose(
        bundle,
        (case,),
        {},
        dialogue,
        config,
        root / "proposal.json",
        wiki_context=wiki.context(),
        wiki_service=wiki.service,
    )
    assert not patches
    report = {
        "mode": "offline_replay",
        "live_smoke": "pending_user_model",
        "questions": 5,
        "statuses": [a.status for a in result.answers],
        "initial_calls": first_calls,
        "reuse_new_calls": len(client.calls) - first_calls,
        "dynamic_dialogue_calls": len(dialogue.calls),
        "evaluation": scores.to_dict(),
        "http_attempts": 0,
        "concurrency": 1,
        "live_limits": {"http_attempts": 80, "active_seconds": 900},
    }
    atomic_json(root / "smoke-report.json", report)
    return report


async def live(
    root,
    model,
    active_seconds=900,
    max_requests=80,
    wiki_max_tokens=12000,
    retry_failed_wiki=False,
    refresh_wiki_reply=False,
):
    """Explicit user-selected connection only. Construction is never model discovery."""
    if not model or not model.strip():
        raise ValueError("Live smoke requires an explicit --model supplied by the user")
    case, snapshot, bundle, spec = prepare_fixture(root, "live")
    transport = Config.from_env(work_dir=root / "transport")
    transport.model_strong = transport.model_middle = transport.model_fast = model
    transport.max_concurrency = transport.fast_max_concurrency = 1
    transport.request_timeout_s = 240
    transport.max_http_requests = max_requests
    transport.request_budget_path = root / "http-attempts.json"
    transport.validate_model()
    config = RunConfig(
        concurrency=1,
        protocol_attempts=3,
        answer_attempts=3,
        tool_steps=8,
        wiki_max_tokens=wiki_max_tokens,
    )
    workspace = Workspace(root / "workspace")
    client = None
    report = {
        "mode": "live",
        "requested_model": model,
        "questions": 5,
        "limits": {
            "http_attempts": max_requests,
            "active_seconds": active_seconds,
            "concurrency": 1,
        },
        "status": "pending",
        "live_smoke": "not_completed",
    }
    try:
        with ActiveBudget(root / "active-budget.json", active_seconds) as budget:
            if budget.remaining <= 0:
                raise ValueError("Active execution budget exhausted; saved progress retained")
            transport.deadline_monotonic = time.monotonic() + budget.remaining
            client = LLMClient(transport)
            async with asyncio.timeout(budget.remaining):
                pipeline = Pipeline(
                    client,
                    root / "generation",
                    frozen_snapshot=snapshot,
                    graph_builder=prepared_graph,
                    embedder_factory=NoEmbedding,
                    workspace=workspace,
                )
                result = await pipeline.run(case, spec, config, execution=ExecutionSelection())
                scores = await ExactFixtureEvaluator().evaluate(result)
                initial_http = client.http_attempts()
                await pipeline.run(
                    case, spec, replace(config, concurrency=2), execution=ExecutionSelection()
                )
                reuse_calls = client.http_attempts() - initial_http
                if reuse_calls:
                    raise AssertionError("Successful answers were called again during continue")
                report.update(
                    live_smoke="generation_completed",
                    statuses=[a.status for a in result.answers],
                    evaluation=scores.to_dict(),
                    reuse_new_http_attempts=reuse_calls,
                )
                atomic_json(root / "smoke-report.json", report)
                wiki = WikiMaintainer(root, "live-five-question", lambda _: client, config)
                await wiki.record(
                    "smoke",
                    "formal",
                    {**asset_evidence(bundle), **_wiki_training_evidence((case,), (result,))},
                )
                service = wiki.service
                session = ProposalSession(
                    root / "proposal.json",
                    payload={
                        "base_version": bundle.version,
                        "case_id": case.id,
                        "asset_ids": [a.id for a in bundle.assets.assets],
                        "wiki": wiki.context(),
                        "goal": "首次必须query_wiki：scope case_ids=[smoke-case], question_ids=[q3], view=regroup，核对分页原件。收到Wiki回复后在同一会话追问、提交补丁或no_change；证据不足明确保留不确定性。",
                    },
                    protocol="Return JSON query_wiki with query(question,scope,view,cursor,max_chars); submit_patch with patches; or no_change with reason and unresolved. Read original evidence and distinguish facts from hypotheses.",
                    workspace=service.workspace,
                )

                if retry_failed_wiki or refresh_wiki_reply:
                    from darwinagent.experiments.wiki_service import WikiQuery

                    seen_jobs = set()
                    for previous in list(session.state["exchanges"]):
                        job_id = previous["reply"].get("job_id")
                        if not job_id or job_id in seen_jobs:
                            continue
                        seen_jobs.add(job_id)
                        job = json.loads((service.root / "jobs" / (job_id + ".json")).read_text())
                        blocked = job.get("blocked", {})
                        retry = (
                            retry_failed_wiki
                            and job["status"] != "complete"
                            and blocked.get("state") == "failed"
                        )
                        refresh = refresh_wiki_reply and job["status"] == "complete"
                        if retry or refresh:
                            if retry:
                                chunk = blocked.get("chunk")
                                service.retry_job(
                                    job_id,
                                    chunks=[chunk] if isinstance(chunk, int) else [],
                                    retry_merge=chunk == "global_merge",
                                )
                            source = (
                                "explicit_failed_wiki_retry"
                                if retry
                                else "explicit_wiki_reply_refresh"
                            )
                            fresh_query = {**previous["query"], "cursor": None}
                            fresh_reply = (await service.query(WikiQuery(**fresh_query))).to_dict()
                            reply_message = {
                                "wiki_reply": fresh_reply,
                                "note": source + "; completed responses and old replies retained.",
                            }
                            if fresh_reply.get("cursor"):
                                reply_message["continuation_query"] = {
                                    **fresh_query,
                                    "cursor": fresh_reply["cursor"],
                                }
                            session.state["exchanges"].append(
                                {"query": fresh_query, "reply": fresh_reply, "source": source}
                            )
                            session.state["messages"].append(
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        reply_message,
                                        ensure_ascii=False,
                                    ),
                                }
                            )
                            session.state["events"].append({"status": source, "job_id": job_id})
                            if session.state["phase"] == "finished":
                                session.state["events"].append(
                                    {
                                        "status": "reopen_after_explicit_wiki_retry",
                                        "previous_action": session.state.pop("action"),
                                    }
                                )
                                session.state.update(phase="ready", format_failures=0)
                            session.save()

                def validate(action):
                    if not session.state["exchanges"] and (
                        action["action"] != "query_wiki"
                        or action["query"].get("view") != "regroup"
                        or action["query"].get("scope", {}).get("question_ids") != ["q3"]
                        or action["query"].get("scope", {})
                        != {"case_ids": [case.id], "question_ids": ["q3"]}
                        or action["query"].get("cursor") is not None
                    ):
                        raise ValueError("First action must regroup original Wiki evidence for q3")
                    if action["action"] == "submit_patch":
                        ProposalGenerator.decode({"patches": action["patches"]}, bundle)
                    return action

                terminal = await session.run(client, config, validate, service)
                job_states = []
                for exchange in session.state["exchanges"]:
                    job_id = exchange["reply"].get("job_id")
                    if job_id:
                        job = json.loads((service.root / "jobs" / f"{job_id}.json").read_text())
                        job_states.append(
                            {"job_id": job_id, "status": job["status"], "error": job.get("error")}
                        )
                regroup_complete = bool(job_states) and all(
                    job["status"] == "complete" for job in job_states
                )
                report.update(
                    status="complete" if regroup_complete else "pending",
                    dynamic_wiki_jobs=job_states,
                    dynamic_reply_statuses=[
                        ex["reply"]["status"] for ex in session.state["exchanges"]
                    ],
                    live_smoke="executed",
                    statuses=[a.status for a in result.answers],
                    evaluation=scores.to_dict(),
                    reuse_new_http_attempts=reuse_calls,
                    terminal_action=terminal,
                    dynamic_exchanges=len(session.state["exchanges"]),
                )
    except Exception as exc:
        report.update(
            status="pending",
            error=f"{type(exc).__name__}: {exc}",
            note="Saved requests, receipts and progress retained; unresolved requests require recovery",
        )
    finally:
        if client is not None:
            report["http_attempts"] = client.http_attempts()
            report["ledger"] = client.ledger_summary()
            await client.aclose()
        atomic_json(root / "smoke-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["replay", "live"], default="replay")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--active-seconds", type=int, default=900)
    parser.add_argument("--max-requests", type=int, default=80)
    parser.add_argument("--wiki-max-tokens", type=int, default=12000)
    parser.add_argument("--retry-failed-wiki", action="store_true")
    parser.add_argument("--refresh-wiki-reply", action="store_true")
    parser.add_argument("--model", help="Required for live: exact user-specified model name")
    args = parser.parse_args()
    if args.mode == "live" and not args.model:
        parser.error(
            "Live requires explicit --model and DARWINAGENT_BASE_URL/DARWINAGENT_API_KEY; no model lookup"
        )
    result = asyncio.run(
        live(
            args.output,
            args.model,
            args.active_seconds,
            args.max_requests,
            args.wiki_max_tokens,
            args.retry_failed_wiki,
            args.refresh_wiki_reply,
        )
        if args.mode == "live"
        else replay(args.output)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
