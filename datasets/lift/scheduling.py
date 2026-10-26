"""Exclusive full-campaign ownership and explicit settled-inflight gate evidence."""

import asyncio
import fnmatch
import hashlib
import io
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import snapshot_files
from darwinagent.runtime.leases import execution_lease

_HELD = ContextVar("lift_campaign_leases", default=frozenset())
AUDIT = "recovery/settled-inflight.json"


@contextmanager
def campaign_lease(root):
    """Nonblocking exclusive ownership; only the exact current task may reenter."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    owner = (str(Path(root).resolve()), threading.get_ident(), id(task))
    if owner in _HELD.get():
        yield
        return
    with execution_lease(Path(root) / "campaign-execution.lock"):
        token = _HELD.set(_HELD.get() | {owner})
        try:
            yield
        finally:
            _HELD.reset(token)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source():
    from src import lift as native

    base = Path(__file__).resolve().parents[2]
    return digest(
        snapshot_files(
            [base / "datasets/lift", base / "src/darwinagent", Path(native.__file__).parents[1]]
        )
    )


def _no_symlinks(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Gate/ledger/audit path must not contain symlinks")


def _verify_historical_source(manifest, expected):
    manifest = Path(manifest)
    _no_symlinks(manifest)
    declaration = json.loads(manifest.read_text())
    files = declaration.get("source_files")
    if not isinstance(files, dict) or not files or declaration.get("source_sha256") != expected:
        raise ValueError("Historical source manifest does not bind original audit")
    if digest(files) != expected:
        raise ValueError("Historical source inventory fingerprint changed")
    for name, checksum in files.items():
        path = Path(name)
        _no_symlinks(path)
        if not path.is_absolute() or _sha(path) != checksum:
            raise ValueError("Historical source file changed")


def record_source_lineage(shared, *, historical_source_manifest):
    """Bind unchanged historical source to current verifier without editing it.

    Does not authorize unknown requests or clear a gate; all original settlement
    and local-proof checks still apply independently.
    """
    with campaign_lease(shared.root), execution_lease(shared.root / "source-lineage.lock"):
        original = shared.root / AUDIT
        _no_symlinks(original)
        audit = json.loads(original.read_text())
        manifest = Path(historical_source_manifest).resolve()
        _verify_historical_source(manifest, audit["source_sha256"])
        current = _source()
        target = shared.root / "recovery/source-lineage" / (current + ".json")
        _no_symlinks(target)
        value = {
            "protocol": "historical-source-lineage-v1",
            "current_source_sha256": current,
            "original_audit_sha256": _sha(original),
            "original_source_sha256": audit["source_sha256"],
            "historical_source_manifest": str(manifest),
            "historical_source_manifest_sha256": _sha(manifest),
        }
        if target.exists():
            if json.loads(target.read_text()) != value:
                raise ValueError("Historical source lineage decision changed")
        else:
            atomic_json(target, value)
        return target


def source_lineage_acknowledged(shared, audit):
    current = _source()
    target = shared.root / "recovery/source-lineage" / (current + ".json")
    if not target.exists():
        return False
    _no_symlinks(target)
    value = json.loads(target.read_text())
    original = shared.root / AUDIT
    _no_symlinks(original)
    manifest = Path(value["historical_source_manifest"])
    _no_symlinks(manifest)
    if (
        value.get("protocol") != "historical-source-lineage-v1"
        or value["current_source_sha256"] != current
        or value["original_audit_sha256"] != _sha(original)
        or value["original_source_sha256"] != audit["source_sha256"]
        or value["historical_source_manifest_sha256"] != _sha(manifest)
    ):
        raise ValueError("Historical source lineage binding changed")
    _verify_historical_source(manifest, audit["source_sha256"])
    return True


def _settled(shared):
    from darwinagent.runtime.model_disposition import ModelDispositionStore

    dispositions = ModelDispositionStore(shared.root)
    _no_symlinks(shared.root)
    records, hashes = [], {}
    for path in sorted((shared.root / "requests").glob("*.json")):
        if path.is_symlink():
            raise ValueError("Request receipt symlinks are not settlement proof")
        row = json.loads(path.read_text())
        count = row.get("actual_http_attempts")
        if (
            (
                row.get("status") not in {"completed", "failed", "budget_exhausted"}
                and not (row.get("status") == "unknown" and dispositions.acknowledged(path))
            )
            or type(count) is not int
            or count < 0
            or (row["status"] == "budget_exhausted" and count != 0)
            or type(row.get("run_id")) is not str
            or not row["run_id"]
        ):
            raise ValueError("Every request must be a typed settled receipt")
        fingerprint = row.get("request_fingerprint")
        if type(fingerprint) is not str or len(fingerprint) != 64:
            raise ValueError("Every settled model receipt requires its request fingerprint")
        try:
            int(fingerprint, 16)
        except ValueError as exc:
            raise ValueError("Invalid model request fingerprint") from exc
        if row["status"] == "completed" and type(row.get("output")) is not str:
            raise ValueError("Completed model receipt requires its settled output")
        records.append(row)
        hashes[path.name] = _sha(path)
    if (
        not records
        or sum(r["actual_http_attempts"] for r in records) != shared.client.http_attempts()
    ):
        raise ValueError("Settled receipts and actual HTTP ledger do not reconcile")
    ledger = Path(shared.cfg.request_budget_path)
    _no_symlinks(ledger)
    if ledger.is_symlink() or json.loads(ledger.read_text()) != {
        "attempts": shared.client.http_attempts()
    }:
        raise ValueError("Actual HTTP ledger does not match the client counter")
    return records, hashes, ledger


def _local_inventory(root):
    """Scan all entries once, retaining the original journal and trace-error scope."""
    pending = [(root, ())]
    steps, trace_failure = [], False
    while pending:
        directory, parents = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError("Every complete local run tree must exist without symlinks")
                relative = parents + (entry.name,)
                path = Path(entry.path)
                if fnmatch.fnmatch(entry.name, "*.json"):
                    if "steps" in relative:
                        steps.append(path)
                    if parents and fnmatch.fnmatch(parents[-1], "trace-errors"):
                        trace_failure = True
                if entry.is_dir(follow_symlinks=False):
                    pending.append((path, relative))
    return steps, trace_failure


def _local_proof(shared, records, run_roots, *, enrolled=None):
    required = {r["run_id"] for r in records}
    paths = (
        list((shared.root / "trajectory-role-enrollments").glob("*.json"))
        if enrolled is None
        else [shared.root / name for name in enrolled]
    )
    for path in paths:
        _no_symlinks(path)
        required.add(json.loads(path.read_text())["experiment_identity"]["run_id"])

    if set(run_roots) != required:
        raise ValueError("Supply every receipt/enrollment run root, including zero-call runs")
    roots, proof = {}, {}
    for run_id, supplied in sorted(run_roots.items()):
        supplied = Path(supplied)
        _no_symlinks(supplied)
        if supplied.is_symlink():
            raise ValueError("Run root symlinks are not audit evidence")
        root = supplied.resolve()
        if not root.is_dir():
            raise ValueError("Every complete local run tree must exist without symlinks")
        steps, trace_failure = _local_inventory(root)
        identity = root / "integration-identity.json"
        if not identity.is_file() or json.loads(identity.read_text()).get("run_id") != run_id:
            raise ValueError("Run root must bind its original integration identity")
        if any(
            root.is_relative_to(Path(old)) or Path(old).is_relative_to(root)
            for old in roots.values()
        ):
            raise ValueError("Run roots must be independent complete trees")
        roots[run_id] = str(root)
        proof[str(identity)] = _sha(identity)
        if any(r["run_id"] == run_id for r in records) and not steps:
            raise ValueError("Receipt-bearing runs require their complete local journals")
        for path in steps:
            raw = path.read_bytes()
            with io.TextIOWrapper(io.BytesIO(raw)) as stream:
                row = json.load(stream)
            state, kind = row.get("state"), row.get("kind")
            if kind not in {"model", "tool"} or state not in {"responded", "failed", "prepared"}:
                raise ValueError("Unknown local model or write cannot be acknowledged")
            if state == "responded" and digest(row.get("response")) != row.get("response_digest"):
                raise ValueError("Local responded receipt digest changed")
            if state == "prepared" and (
                kind != "model"
                or row.get("pending_budget") != "Native scheduling paused: unknown"
                or any(key in row for key in ("request_id", "response", "response_digest"))
            ):
                raise ValueError("Only a model prepared at this scheduling gate is eligible")
            proof[str(path)] = hashlib.sha256(raw).hexdigest()
        if trace_failure:
            raise ValueError("Unresolved local trace failure remains")
    return roots, proof


def recover_settled_inflight(shared, *, run_roots, expected_http_attempts, expected_pause_sha256):
    """Append one exact audit; never edit pause, receipts, local steps or counters."""
    with campaign_lease(shared.root), execution_lease(shared.root / "recovery.lock"):
        pause = shared.root / "pause.json"
        _no_symlinks(pause)
        _no_symlinks(shared.root / AUDIT)
        if (
            not pause.is_file()
            or _sha(pause) != expected_pause_sha256
            or json.loads(pause.read_text()).get("status") != "unknown"
            or (shared.root / "active-pause.json").exists()
            or list((shared.root / "trace-errors").glob("*.json"))
        ):
            raise ValueError("Only the exact inactive unknown scheduling pause can be audited")
        if (
            type(expected_http_attempts) is not int
            or shared.client.http_attempts() != expected_http_attempts
        ):
            raise ValueError("Actual HTTP budget counter changed")
        records, receipts, ledger = _settled(shared)
        roots, proof = _local_proof(shared, records, run_roots)
        workers = shared.root / "worker-calls.json"
        counts = json.loads(workers.read_text()) if workers.exists() else {}
        if any(type(v) is not int or v < 0 for v in counts.values()):
            raise ValueError("Worker counters must remain typed cumulative counts")
        expected_counts = {}
        for row in records:
            if row.get("role") == "worker":
                session = row.get("session")
                if type(session) is not str or not session:
                    raise ValueError("Worker receipt requires its exact session")
                expected_counts[session] = expected_counts.get(session, 0) + 1
        if {name: count for name, count in counts.items() if count} != expected_counts:
            raise ValueError("Every cumulative worker counter must reconcile to settled receipts")
        enrollments = {
            str(p.relative_to(shared.root)): _sha(p)
            for p in sorted((shared.root / "trajectory-role-enrollments").glob("*.json"))
        }
        model_proof = {
            p.name: {
                "request_fingerprint": row["request_fingerprint"],
                "response_sha256": hashlib.sha256(row["output"].encode()).hexdigest()
                if row["status"] == "completed"
                else None,
            }
            for p, row in zip(sorted((shared.root / "requests").glob("*.json")), records)
        }
        audit = {
            "protocol": "settled-inflight-recovery-v1",
            "reason": "settled_inflight_scheduling_conflict",
            "pause_sha256": expected_pause_sha256,
            "http_attempts": expected_http_attempts,
            "http_ledger": str(ledger.resolve()),
            "http_ledger_sha256": _sha(ledger),
            "policy_sha256": _sha(shared.root / "policy.json"),
            "settled_receipts": receipts,
            "model_receipt_proof": model_proof,
            "enrollments": enrollments,
            "run_roots": roots,
            "local_proof": proof,
            "worker_counts": counts,
            "source_sha256": _source(),
        }
        target = shared.root / AUDIT
        if target.exists():
            if json.loads(target.read_text()) != audit:
                raise ValueError("Dedicated settlement audit evidence changed")
        else:
            atomic_json(target, audit)
        return audit


def settled_inflight_acknowledged(shared, pause):
    target = shared.root / AUDIT
    if not target.exists() or pause.name != "pause.json":
        return False
    _no_symlinks(target)
    _no_symlinks(pause)
    audit = json.loads(target.read_text())
    if (
        audit.get("protocol") != "settled-inflight-recovery-v1"
        or audit.get("reason") != "settled_inflight_scheduling_conflict"
        or not pause.exists()
        or _sha(pause) != audit["pause_sha256"]
        or _sha(shared.root / "policy.json") != audit["policy_sha256"]
        or (_source() != audit["source_sha256"] and not source_lineage_acknowledged(shared, audit))
        or shared.client.http_attempts() < audit["http_attempts"]
    ):
        raise ValueError("Dedicated settled-inflight recovery binding changed")
    records, receipts, ledger = _settled(shared)
    if str(ledger.resolve()) != audit["http_ledger"]:
        raise ValueError("Actual HTTP ledger identity changed")
    if any(receipts.get(name) != checksum for name, checksum in audit["settled_receipts"].items()):
        raise ValueError("Original settled receipt changed")
    if any(_sha(shared.root / name) != checksum for name, checksum in audit["enrollments"].items()):
        raise ValueError("Original enrollment evidence changed")
    roots, proof = _local_proof(
        shared,
        [r for r in records if r["run_id"] in audit["run_roots"]],
        audit["run_roots"],
        enrolled=audit["enrollments"],
    )
    if roots != audit["run_roots"] or proof != audit["local_proof"]:
        raise ValueError("Original local journal proof changed")
    counts = (
        json.loads((shared.root / "worker-calls.json").read_text())
        if (shared.root / "worker-calls.json").exists()
        else {}
    )
    if any(counts.get(name, -1) < count for name, count in audit["worker_counts"].items()):
        raise ValueError("Cumulative worker counters were reset")
    return True
