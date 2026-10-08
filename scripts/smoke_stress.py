"""Twelve new live regression cases and three independent Wiki proposal sessions.

References remain in this controller. Prepared graphs test the public retrieval,
answer, review, continuation and Wiki chain, not extraction or embedding quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import smoke_extended as extended
import smoke_intervention as base

from darwinagent.config import Config, RunConfig
from darwinagent.engine import Pipeline
from darwinagent.experiments.bootstrap import revision_protocol
from darwinagent.experiments.proposal_session import ProposalSession
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.kernel.assets import KernelBundle
from darwinagent.kernel.revision import AssetRevisionService, parse_training_id, training_id
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.execution import ActiveBudget, ExecutionSelection
from darwinagent.runtime.steps import UnknownRequest
from darwinagent.runtime.workspace import Workspace


def definitions():
    def q(number, text, serial, expected):
        return (f"q{number}", text, serial, expected)

    def daily(serial, count, technician):
        return [
            (serial, str(date(2026, 1, 1) + timedelta(days=i)), technician) for i in range(count)
        ]

    return [
        (
            "shuffled-dates",
            [
                ("D-81", "2026-05-12", "李"),
                ("D-81", "2026-01-03", "赵"),
                ("D-81", "2026-03-04", "林"),
            ],
            [
                q(
                    1,
                    "D-81最早的维护事件由谁在何时执行？返回technician和date。",
                    "D-81",
                    {"technician": "赵", "date": "2026-01-03"},
                ),
                q(
                    2,
                    "D-81最后一次维护由谁在何时执行？返回technician和date。",
                    "D-81",
                    {"technician": "李", "date": "2026-05-12"},
                ),
                q(3, "D-81共有多少条维护记录？返回count。", "D-81", {"count": 3}),
            ],
        ),
        (
            "serial-prefix",
            [
                ("D-1", "2026-02-01", "甲"),
                ("D-10", "2026-02-02", "乙"),
                ("D-100", "2026-02-03", "丙"),
            ],
            [
                q(
                    1,
                    "D-1由谁维护？设备编号必须精确匹配，返回technician。",
                    "D-1",
                    {"technician": "甲"},
                ),
                q(
                    2,
                    "D-100由谁维护？返回technician和date。",
                    "D-100",
                    {"technician": "丙", "date": "2026-02-03"},
                ),
                q(3, "D-1000由谁维护？没有该设备记录应abstained。", "D-1000", None),
            ],
        ),
        (
            "same-date-subjects",
            [("D-82", "2026-07-10", "刘"), ("D-83", "2026-07-10", "周")],
            [
                q(
                    1,
                    "2026-07-10谁维护D-83？返回technician和date。",
                    "D-83",
                    {"technician": "周", "date": "2026-07-10"},
                ),
                q(
                    2,
                    "2026-07-10谁维护D-82？返回technician和date。",
                    "D-82",
                    {"technician": "刘", "date": "2026-07-10"},
                ),
            ],
        ),
        (
            "exact-page-20",
            daily("D-84", 20, "汪"),
            [
                q(
                    1,
                    "D-84共有多少条记录？使用f_page_facts，offset=0，limit=20，核对是否结束，返回count。",
                    "D-84",
                    {"count": 20},
                ),
                q(
                    2,
                    "D-84最后一条记录的日期和技术员？返回date和technician。",
                    "D-84",
                    {"date": "2026-01-20", "technician": "汪"},
                ),
            ],
        ),
        (
            "one-past-page",
            daily("D-85", 20, "杨") + [("D-85", "2026-01-21", "邹")],
            [
                q(
                    1,
                    "D-85共有多少条记录？用f_page_facts每页limit=20，从0读到more_remain=false，返回count。",
                    "D-85",
                    {"count": 21},
                ),
                q(
                    2,
                    "D-85最后一次由谁在何时维护？不能只看第一页，返回technician和date。",
                    "D-85",
                    {"technician": "邹", "date": "2026-01-21"},
                ),
            ],
        ),
        (
            "filtered-tail",
            daily("D-90", 64, "曹") + [("D-91", "2026-02-01", "余"), ("D-91", "2026-02-02", "马")],
            [
                q(
                    1,
                    "D-91共有多少条记录？原文前半段是别的设备，按完整设备编号过滤，返回count。",
                    "D-91",
                    {"count": 2},
                ),
                q(
                    2,
                    "D-91最后一次维护的技术员和日期？返回technician和date。",
                    "D-91",
                    {"technician": "马", "date": "2026-02-02"},
                ),
            ],
        ),
        (
            "interleaved-devices",
            [
                (serial, str(date(2026, 1, 1) + timedelta(days=i)), technician)
                for i in range(24)
                for serial, technician in [("D-92", "赵"), ("D-93", "吴")]
            ],
            [
                q(
                    1,
                    "交替记录中D-92有多少条？只统计D-92，用f_page_facts跨页核对，返回count。",
                    "D-92",
                    {"count": 24},
                ),
                q(
                    2,
                    "D-93最后一条记录的日期和技术员？返回date和technician。",
                    "D-93",
                    {"date": "2026-01-24", "technician": "吴"},
                ),
            ],
        ),
        (
            "technician-change",
            [
                ("D-94", "2026-01-01", "王"),
                ("D-94", "2026-01-02", "李"),
                ("D-94", "2026-01-03", "王"),
            ],
            [
                q(
                    1,
                    "完整读取D-94全部记录，列出所有不同技术员，返回technicians数组，不得重复。",
                    "D-94",
                    {"technicians": ["李", "王"]},
                ),
                q(
                    2,
                    "2026-01-02谁维护D-94？返回technician和date。",
                    "D-94",
                    {"technician": "李", "date": "2026-01-02"},
                ),
                q(
                    3,
                    "D-94有多少条记录？不要把技术员人数当记录数，返回count。",
                    "D-94",
                    {"count": 3},
                ),
            ],
        ),
        (
            "year-boundary",
            [
                ("D-95", "2025-12-31", "周"),
                ("D-95", "2026-01-01", "郑"),
                ("D-95", "2026-01-02", "周"),
            ],
            [
                q(
                    1,
                    "D-95最早记录的日期和技术员？注意跨年，返回date和technician。",
                    "D-95",
                    {"date": "2025-12-31", "technician": "周"},
                ),
                q(
                    2,
                    "D-95最后记录的日期和技术员？返回date和technician。",
                    "D-95",
                    {"date": "2026-01-02", "technician": "周"},
                ),
                q(
                    3,
                    "D-95全部不同技术员有哪些？返回去重technicians数组。",
                    "D-95",
                    {"technicians": ["周", "郑"]},
                ),
            ],
        ),
        (
            "leap-day",
            [
                ("D-96", "2024-02-28", "何"),
                ("D-96", "2024-02-29", "蒋"),
                ("D-96", "2024-03-01", "何"),
            ],
            [
                q(
                    1,
                    "2024-02-29是谁维护D-96？返回technician和date。",
                    "D-96",
                    {"technician": "蒋", "date": "2024-02-29"},
                ),
                q(
                    2,
                    "D-96最早一次维护的日期和技术员？返回date和technician。",
                    "D-96",
                    {"date": "2024-02-28", "technician": "何"},
                ),
            ],
        ),
        (
            "empty-match",
            [("D-97", "2026-05-05", "莫")],
            [
                q(1, "谁维护D-98？使用f_page_facts确认记录，没有证据则abstained。", "D-98", None),
                q(
                    2,
                    "谁在何时维护D-97？返回technician和date。",
                    "D-97",
                    {"technician": "莫", "date": "2026-05-05"},
                ),
            ],
        ),
        (
            "out-of-range-recovery",
            daily("D-99", 10, "韩"),
            [
                q(
                    1,
                    "先用f_page_facts(offset=99,limit=20)检查D-99的越界页，再从offset=0重新读取，不能把越界空页当无记录。返回总count。",
                    "D-99",
                    {"count": 10},
                ),
                q(
                    2,
                    "D-99最早记录的日期和技术员？返回date和technician。",
                    "D-99",
                    {"date": "2026-01-01", "technician": "韩"},
                ),
            ],
        ),
    ]


def audit_saved_answers(root, case_definitions=None):
    """Independently audit completed answers, including partly finished cases."""
    rows = []
    missing = []
    selected = definitions() if case_definitions is None else case_definitions
    for case_id, _, questions in selected:
        saved = {}
        for path in (root / "generation" / case_id).glob("branches/main/answers/*.json"):
            record = json.loads(path.read_text())
            if digest(record["result"]) != record["digest"]:
                raise ValueError(f"Answer checksum mismatch: {path}")
            case = json.loads(Path(record["case_path"]).read_text())
            if digest(case) != record["case_digest"]:
                raise ValueError(f"Case checksum mismatch: {path}")
            graph = json.loads(Path(record["graph_path"]).read_text())
            if digest(graph) != record["graph_fingerprint"]:
                raise ValueError(f"Graph checksum mismatch: {path}")
            saved[record["result"]["question_id"]] = (record, case)
        for qid, text, serial, expected in questions:
            if qid not in saved:
                missing.append({"case_id": case_id, "question_id": qid})
                continue
            record, case = saved[qid]
            original = next(q for q in case["questions"] if q["id"] == qid)
            if original["text"] != text or original["parameters"] != {"serial": serial}:
                raise ValueError(f"Question version mismatch: {case_id}/{qid}")
            answer = record["result"]
            result = SimpleNamespace(answers=[SimpleNamespace(**answer)])
            field_result = extended.evaluate(result, {qid: expected})["rows"][0]
            tools = []
            for event in answer["trace"]:
                if event.get("stage") != "tool":
                    continue
                data = event["data"]
                items = data.get("rows", []) if isinstance(data, dict) else data
                tools.append(
                    {
                        "asset_id": event["asset_id"],
                        "parameters": event["parameters"],
                        "returned_rows": len(items),
                        "control": {k: v for k, v in data.items() if k != "rows"}
                        if isinstance(data, dict)
                        else {},
                        "read_node_count": len(event.get("read_node_ids", [])),
                        "returned_node_count": len(event.get("node_ids", [])),
                    }
                )
            rows.append({"case_id": case_id, **field_result, "tools": tools})
    report = {
        "expected_cases": len(selected),
        "expected_questions": sum(len(case[2]) for case in selected),
        "completed_questions": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "missing": missing,
        "rows": rows,
    }
    atomic_json(root / "saved-answer-audit.json", report)
    return report


async def wiki_checks(
    root,
    model,
    active_seconds,
    max_requests,
    *,
    case_definitions=None,
    wiki_case_ids=None,
    concurrency=1,
    identity="stress-twelve",
    tool_steps=10,
):
    cfg = Config.from_env(work_dir=root / "transport")
    cfg.model_strong = cfg.model_middle = cfg.model_fast = model
    cfg.max_concurrency = cfg.fast_max_concurrency = concurrency
    cfg.max_retries = 1
    cfg.request_timeout_s = 240
    cfg.max_http_requests = max_requests
    cfg.request_budget_path = root / "http-attempts.json"
    cfg.validate_model()
    config = RunConfig(
        concurrency=concurrency,
        protocol_attempts=3,
        answer_attempts=3,
        tool_steps=tool_steps,
        wiki_max_tokens=12000,
    )
    workspace = Workspace(root / "workspace")
    rows = []
    client = None
    selected = definitions() if case_definitions is None else case_definitions
    wiki_case_ids = (
        {"one-past-page", "filtered-tail", "technician-change"}
        if wiki_case_ids is None
        else set(wiki_case_ids)
    )
    try:
        with ActiveBudget(root / "active-budget.json", active_seconds) as budget:
            cfg.deadline_monotonic = time.monotonic() + budget.remaining
            client = base.LLMClient(cfg)
            from smoke_transport_audit import instrument_pool

            transport_audit = instrument_pool(client, root / "wiki-transport-concurrency.json")
            wiki = WikiMaintainer(root, identity, lambda _: client, config)
            async with asyncio.timeout(budget.remaining):
                for case, snapshot, bundle, task, _ in extended.fixtures(root, selected):
                    if case.id not in wiki_case_ids:
                        continue
                    qid = case.questions[0].id
                    start = client.http_attempts()
                    pipeline = Pipeline(
                        client,
                        root / "generation",
                        frozen_snapshot=snapshot,
                        graph_builder=base.prepared_graph,
                        embedder_factory=base.NoEmbedding,
                        workspace=workspace,
                    )
                    try:
                        result = await pipeline.run(
                            case, task, config, execution=ExecutionSelection(question_ids=(qid,))
                        )
                    except UnknownRequest as exc:
                        rows.append({"case_id": case.id, "status": "pending", "error": str(exc)})
                        atomic_json(root / "wiki-stress-report.json", {"sessions": rows})
                        continue
                    assert client.http_attempts() == start, (
                        "Wiki preparation unexpectedly regenerated answers"
                    )
                    await wiki.record(
                        case.id,
                        "formal",
                        {
                            **base.asset_evidence(bundle),
                            **base._wiki_training_evidence((case,), (result,)),
                        },
                    )
                    scope = {"case_ids": [case.id], "question_ids": [qid]}
                    allowed_evidence = (training_id(case.id, qid),)
                    assets = [
                        dict(a.to_dict(), fingerprint=a.fingerprint, current_ref="current:" + a.id)
                        for a in bundle.assets.assets
                    ]
                    session = ProposalSession(
                        root / f"proposal-{case.id}.json",
                        payload={
                            "base_version": bundle.version,
                            "goal": f"先查询Wiki重新归纳{qid}原件，再核对参数、返回数据及覆盖。之后在同一会话继续，证据不足保留不确定性。",
                            "scope": scope,
                            "assets": assets,
                            "questions": [
                                {"training_id": allowed_evidence[0], "text": case.questions[0].text}
                            ],
                            "task_output": {
                                "answer_format": task.answer_format,
                                "answer_contract": task.answer_contract,
                            },
                            "wiki": wiki.context(max_chars=6000),
                        },
                        protocol=revision_protocol(bundle)
                        + "\nExisting asset base_fingerprint may use current:<asset_id>, resolved against the frozen bundle. "
                        + "Return JSON query_wiki, submit_patch or no_change. First action must query_wiki with view=regroup and the supplied exact scope. Cite evidence; do not equate page limit with returned count. submit_patch changes S/F/C/P assets, not Wiki entries. If evidence supports no asset change, explain no_change.",
                        workspace=workspace,
                    )

                    def validate(
                        action,
                        session=session,
                        scope=scope,
                        bundle=bundle,
                        allowed_evidence=allowed_evidence,
                    ):
                        if not session.state["exchanges"] and (
                            action["action"] != "query_wiki"
                            or action["query"].get("scope") != scope
                            or action["query"].get("view") != "regroup"
                        ):
                            raise ValueError(
                                "First action must query_wiki/regroup with the supplied exact scope"
                            )
                        if action["action"] == "submit_patch":
                            patches = base.ProposalGenerator.decode(
                                {"patches": action["patches"]}, bundle
                            )
                            for patch in patches:
                                for evidence in patch.training_evidence:
                                    parse_training_id(evidence)
                                if set(patch.training_evidence) - set(allowed_evidence):
                                    raise ValueError("Patch cites evidence outside the query scope")
                        return action

                    if session.state["phase"] == "finished":
                        try:
                            validate(session.state["action"])
                        except (ValueError, TypeError, KeyError) as exc:
                            session.state["events"].append(
                                {"status": "rejected_terminal", "action": session.state["action"]}
                            )
                            session.feedback(
                                "Saved terminal action fails the asset patch protocol: "
                                + str(exc)
                                + ". submit_patch changes S/F/C/P assets, not Wiki entries. "
                                + "Actual frozen assets: "
                                + json.dumps(assets, ensure_ascii=False)
                                + ". Allowed training_evidence identifiers: "
                                + json.dumps(allowed_evidence)
                                + ". Submit a valid asset patch, query Wiki, or explain no_change."
                            )
                    action = await session.run(client, config, validate, wiki.service)
                    validate(action)
                    candidate = None
                    if action["action"] == "submit_patch":
                        target = root / "candidates" / case.id / digest(action)
                        patches = base.ProposalGenerator.decode(
                            {"patches": action["patches"]}, bundle
                        )
                        if not target.exists():
                            admitted = AssetRevisionService().propose(
                                bundle, patches, target, allowed_evidence
                            )
                            candidate = {"version": admitted.version, "admission": "pass"}
                        else:
                            admitted = KernelBundle(target / "bundle")
                            admitted.verify()
                            candidate = {"version": admitted.version, "admission": "pass"}
                    jobs = []
                    for exchange in session.state["exchanges"]:
                        identifier = exchange["reply"].get("job_id")
                        if identifier:
                            job = json.loads(
                                (wiki.service.root / "jobs" / f"{identifier}.json").read_text()
                            )
                            jobs.append(
                                {
                                    "id": identifier,
                                    "status": job["status"],
                                    "chunks": len(job["completed"]),
                                    "error": job.get("error"),
                                }
                            )
                    rows.append(
                        {
                            "case_id": case.id,
                            "jobs": jobs,
                            "action": action,
                            "candidate": candidate,
                            "exchanges": len(session.state["exchanges"]),
                            "status": "complete"
                            if jobs and all(j["status"] == "complete" for j in jobs)
                            else "pending",
                        }
                    )
                    atomic_json(root / "wiki-stress-report.json", {"sessions": rows})
    except Exception as exc:
        rows.append({"status": "pending", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        report = {
            "sessions": rows,
            "status": "complete"
            if len(rows) == len(wiki_case_ids) and all(row["status"] == "complete" for row in rows)
            else "pending",
        }
        if client:
            report.update(
                http_attempts=client.http_attempts(),
                ledger=client.ledger_summary(),
                transport_peak=transport_audit["peak"],
            )
            await client.aclose()
        atomic_json(root / "wiki-stress-report.json", report)
    return report


async def live(root, model, active_seconds=3600, max_requests=240):
    report = await extended.live(
        root, model, active_seconds, max_requests, definitions=definitions()
    )
    wiki = await wiki_checks(root, model, active_seconds, max_requests)
    audit = audit_saved_answers(root)
    summary = {
        "model": model,
        "concurrency": 1,
        "cases": len(report["cases"]),
        "questions": sum(c["evaluation"]["total"] for c in report["cases"]),
        "passed": sum(c["evaluation"]["passed"] for c in report["cases"]),
        "saved_answer_audit": {k: v for k, v in audit.items() if k != "rows"},
        "generation": report["status"],
        "wiki": wiki["status"],
        "http_attempts": wiki.get("http_attempts", report.get("http_attempts")),
        "status": "complete" if report["status"] == wiki["status"] == "complete" else "pending",
    }
    atomic_json(root / "stress-report.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--active-seconds", type=int, default=3600)
    parser.add_argument("--max-requests", type=int, default=240)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(live(args.output, args.model, args.active_seconds, args.max_requests)),
            ensure_ascii=False,
        )
    )
