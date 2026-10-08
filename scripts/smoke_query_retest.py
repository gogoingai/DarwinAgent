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
        exact_query = any(e["query"] == required for e in evidence)
        report = {
            "status": "complete" if evidence and wiki_calls and exact_query else "partial",
            "parent_session": str(original),
            "base_version": payload["base_version"],
            "queries": len(exchanges),
            "same_query_retested": exact_query,
            "wiki_model_calls": len(wiki_calls),
            "http_attempts": transport.client.http_attempts(),
            "action": action["action"],
            "replies": [e["reply"] for e in exchanges],
            "transport_peak": transport.concurrency_state["peak"],
            "candidate_is_draft": True,
            "wiki_thinking_disabled": True,
        }
        atomic_json(folder / "report.json", report)
        return report
    finally:
        await transport.close()
