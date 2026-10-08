"""Real public loop acceptance with a durable-response interruption and resume."""

import json
from pathlib import Path

from smoke_live_loop import MODEL, execute

from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.steps import StepJournal


async def live(root, *, rounds=2, active_seconds=3600, max_requests=300):
    root = Path(root).resolve()
    marker = root / "interrupt-injection.json"
    original = StepJournal.respond

    def interrupt(journal, path, response):
        original(journal, path, response)
        if not marker.exists() and response.get("role") == "tools" and "B0/generation" in str(path):
            row = json.loads(path.read_text())
            atomic_json(marker, {"path": str(path), "row": row})
            raise RuntimeError("Acceptance interruption after durable response commit")

    options = dict(
        mode="live",
        model=MODEL,
        rounds=rounds,
        active_seconds=active_seconds,
        max_requests=max_requests,
    )
    if not marker.exists():
        StepJournal.respond = interrupt
        try:
            first = await execute(root, **options)
            atomic_json(root / "interrupted-loop-report.json", first)
        finally:
            StepJournal.respond = original
        if not marker.exists():
            raise AssertionError("Formal B0 response interruption did not execute")
    report = await execute(root, resume=True, **options)
    injected = json.loads(marker.read_text())
    restored = json.loads(Path(injected["path"]).read_text())
    receipt_reused = all(
        restored[key] == injected["row"][key]
        for key in ("request_id", "response_digest", "response")
    )
    if not receipt_reused:
        raise AssertionError("Interrupted response or request identity changed")
    report["interrupted_response_reused"] = receipt_reused
    atomic_json(root / "durable-loop-report.json", report)
    if report["loop_proven"]:
        cached = await execute(root, resume=True, **options)
        atomic_json(root / "completed-resume-report.json", cached)
        if cached["status"] != "complete" or not cached["loop_proven"] or cached["error"]:
            raise AssertionError("Completed loop resume did not complete successfully")
        report["completed_resume_http_attempts"] = cached["http_attempts"] - report["http_attempts"]
        if report["completed_resume_http_attempts"] != 0:
            raise AssertionError("Completed loop resume dispatched model requests")
        atomic_json(root / "durable-loop-report.json", report)
    return report
