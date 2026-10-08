"""Continue the saved failed-scope proposal dialogue without rewriting its history."""

import asyncio
import json
import time
from pathlib import Path

from darwinagent.config import Config, RunConfig
from darwinagent.experiments.proposal_session import ProposalSession, decode_action
from darwinagent.experiments.wiki_service import WikiService
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.execution import ActiveBudget

try:
    from .smoke_live_loop import MODEL, AuditTransport
except ImportError:
    from smoke_live_loop import MODEL, AuditTransport


async def live(root, active_seconds=3600):
    root = Path(root).resolve()
    original = root / "R3/optimization/attempt-0/proposal-call.json"
    prior = json.loads(original.read_text())
    required = prior["exchanges"][0]["query"]
    folder = root / "query-retest"
    folder.mkdir(parents=True, exist_ok=True)
    cfg = Config.from_env(work_dir=folder / "transport")
    cfg.model_strong = cfg.model_middle = cfg.model_fast = MODEL
    cfg.max_concurrency = cfg.fast_max_concurrency = 2
    cfg.max_retries = 1
    cfg.model_profiles = True
    cfg.thinking_disabled_roles.add("wiki_maintainer")
    cfg.max_http_requests = 80
    cfg.request_budget_path = folder / "http-attempts.json"
    cfg.validate_model()
    transport = AuditTransport(folder, cfg)
    run = RunConfig(protocol_attempts=2, wiki_max_tokens=10000)
    service = WikiService(root, lambda: transport.for_stage("query-retest"), run)
    payload = {
        **prior["input"],
        "required_query": required,
        "objective": "此前补查因组合题号未被解析而错误返回空覆盖，没有实际归纳。"
        "该接口现已修复。先原样提交 required_query 的 query_wiki，等待新的原件归纳回复，"
        "再继续原会话的候选审查；不强行修改，也不根据旧空回复推断不存在证据。",
    }
    session = ProposalSession(
        folder / "proposal-call.json",
        payload=payload,
        protocol=prior["protocol"],
        previous=original,
        workspace=service.workspace,
    )
    try:
        with ActiveBudget(folder / "active-budget.json", active_seconds) as budget:
            cfg.deadline_monotonic = time.monotonic() + budget.remaining
            async with asyncio.timeout(budget.remaining):
                fresh = await service.query(required)
                regroup = json.loads((service.root / "jobs" / (fresh.job_id + ".json")).read_text())
                if regroup["status"] != "complete":
                    raise RuntimeError(
                        "Explicit Wiki regroup retry still incomplete; stop before proposal calls"
                    )
                marker = folder / "fresh-regroup-recovery.json"
                if not marker.exists():
                    session.state["messages"].append(
                        {
                            "role": "user",
                            "content": "人工恢复通知：旧 cursor 绑定失败时的回复，已显式重试 Wiki 汇总。请先原样 query required_query（cursor=null）读取新结果，再继续判断；不要继续旧 cursor。",
                        }
                    )
                    session.state["events"].append(
                        {"status": "human_wiki_recovery", "job_id": fresh.job_id}
                    )
                    if session.state["phase"] == "submitted":
                        replacement = service.workspace.replacement_for(session.state["request_id"])
                        if replacement is None or replacement["status"] != "prepared":
                            raise ValueError(
                                "Recover or explicitly retry submitted proposal before revising its input"
                            )
                        service.workspace.revise_prepared_request(
                            replacement["id"],
                            {
                                "role": run.proposal_role,
                                "messages": session.state["messages"],
                                "base_version": session.state["input"]["base_version"],
                                "session": str(session.target),
                            },
                        )
                    session.save()
                    atomic_json(marker, {"job_id": fresh.job_id, "status": fresh.status})
                if session.state["phase"] == "wiki_paused":
                    session.state["events"].append(
                        {
                            "status": "human_reset_wiki_cursor",
                            "previous_query": session.state["action"]["query"],
                            "query": required,
                        }
                    )
                    session.state["action"]["query"] = required
                    session.retry_wiki(
                        "Explicit merge retry completed; same model Wiki thinking disabled"
                    )
                action = await session.run(
                    transport.for_stage("query-retest"), run, decode_action, service
                )
        exchanges = session.state["exchanges"]
        evidence = [
            e
            for e in exchanges
            if e["reply"]["status"] == "complete"
            and e["reply"]["evidence_refs"]
            and e["reply"]["covered"]
        ]
        wiki_calls = [c for c in transport.calls if c["request"]["role"] == "wiki_maintainer"]

        def completed_reply(exchange):
            if exchange["query"] != required:
                return False
            data = exchange["reply"]
            cursor = data.get("cursor")
            if cursor:
                token = json.loads(cursor)
                if "reply" in token:
                    data = json.loads(
                        (service.root / "replies" / (token["reply"] + ".json")).read_text()
                    )["data"]
            return (
                data.get("status") == "complete"
                and bool(data.get("evidence_refs"))
                and bool(data.get("covered"))
            )

        exact_query = any(completed_reply(e) for e in exchanges)
        jobs = [json.loads(p.read_text()) for p in service.root.glob("jobs/*.json")]
        completed_jobs = [
            j
            for j in jobs
            if j.get("status") == "complete"
            and j.get("merged")
            and len(j.get("completed", {})) == len(j.get("chunks", []))
        ]
        paged_complete = any(
            e["reply"].get("job_id") in {j["snapshot"] for j in completed_jobs} for e in exchanges
        )
        report = {
            "status": "complete"
            if (evidence or paged_complete) and wiki_calls and exact_query
            else "partial",
            "parent_session": str(original),
            "base_version": payload["base_version"],
            "queries": len(exchanges),
            "same_query_retested": exact_query,
            "fresh_complete_reply_delivered_in_same_session": exact_query,
            "wiki_model_calls": len(wiki_calls),
            "http_attempts": transport.client.http_attempts(),
            "action": action["action"],
            "replies": [e["reply"] for e in exchanges],
            "transport_peak": transport.concurrency_state["peak"],
            "candidate_is_draft": True,
            "wiki_thinking_disable_requested": True,
            "model_profiles_enabled_on_final_retry": True,
            "completed_regroup_jobs": [
                {
                    "evidence_version": j["snapshot"],
                    "covered_chunks": len(j["completed"]),
                    "total_chunks": len(j["chunks"]),
                    "merged": True,
                }
                for j in completed_jobs
            ],
        }
        atomic_json(folder / "report.json", report)
        return report
    finally:
        await transport.close()
