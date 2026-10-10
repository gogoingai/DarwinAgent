"""Synthetic shared-policy history for offline adapter tests; no real calls or services."""

from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.transport import SharedTransport


def worker16_fixture(client, root, *, attempts=0, amend_worker=True):
    """Audit synthetic history through the production APIs without HTTP dispatch."""
    root = Path(root)
    client.cfg.max_http_requests = 100
    client.cfg.request_budget_path = root.parent / "http-attempts.json"
    cap_start = min(attempts, 78)
    atomic_json(client.cfg.request_budget_path, {"attempts": cap_start})
    shared = SharedTransport(client, root)
    for n in range(max(1, cap_start)):
        atomic_json(
            root / "requests" / f"fixture-{n:03}.json",
            {
                "status": "completed",
                "actual_http_attempts": 1 if cap_start else 0,
                "run_id": "synthetic-history",
                "role": "worker",
                "usage": {},
                "started": 0,
                "finished": 0,
            },
        )
    atomic_json(root / "worker-calls.json", {"historical-worker": 8})
    shared.amend_http_cap(total_cap=500, expected_http_attempts=cap_start)
    client.cfg.max_http_requests = 500
    shared = SharedTransport(client, root)
    for n in range(cap_start, attempts):
        atomic_json(
            root / "requests" / f"fixture-{n:03}.json",
            {
                "status": "completed",
                "actual_http_attempts": 1,
                "run_id": "synthetic-history",
                "role": "worker",
                "usage": {},
                "started": 0,
                "finished": 0,
            },
        )
    atomic_json(client.cfg.request_budget_path, {"attempts": attempts})
    if amend_worker:
        shared.amend_worker_limit(worker_limit=16, expected_http_attempts=attempts)
        return SharedTransport(client, root, worker_limit=16)
    return shared
