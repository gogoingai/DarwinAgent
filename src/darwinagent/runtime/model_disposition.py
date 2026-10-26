"""Additive, explicitly authorized disposition of one unknown model request.

Only a trusted human-authorized host operation may call record(). This store
never retries, changes original receipts, or clears a pause. Transport consumers
must still reject other unknown requests and independently validate run identity.
"""

import hashlib
import json
import stat
from functools import lru_cache
from pathlib import Path

from .artifacts import atomic_json, digest
from .leases import execution_lease


def _sha(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Recovery evidence cannot be a symlink")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _step_identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Recovery step must be a regular file, never a symlink")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@lru_cache(maxsize=32768)
def _cached_step_state(filename, identity):
    # Retain only two lifecycle fields, never large model inputs or tool outputs.
    path = Path(filename)
    item = json.loads(path.read_text())
    if _step_identity(path) != identity:
        raise ValueError("Recovery step changed during validation")
    return item.get("kind"), item.get("state")


def _step_state(path):
    # Re-stat every call: ctime catches same-size edits even when mtime is restored.
    identity = _step_identity(path)
    result = _cached_step_state(str(path), identity)
    if _step_identity(path) != identity:
        raise ValueError("Recovery step changed during validation")
    return result


class ModelDispositionStore:
    def __init__(self, root):
        root = Path(root)
        if root.is_symlink():
            raise ValueError("Shared root cannot be a symlink")
        self.root = root.resolve()
        self.folder = self.root / "recovery" / "model-dispositions"
        self.ledger = self.root.parent / "http-attempts.json"

    def _safe_folder(self):
        for path in (self.root / "requests", self.root / "recovery", self.folder):
            if path.is_symlink():
                raise ValueError("Recovery folders cannot be symlinks")

    def _receipt(self, path):
        self._safe_folder()
        path = Path(path)
        if path.is_symlink() or path.resolve().parent != self.root / "requests":
            raise ValueError("Receipt must belong to shared request directory")
        row = json.loads(path.read_text())
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("Receipt requires a typed request identity")
        if path.name != row["id"] + ".json":
            raise ValueError("Receipt identity mismatch")
        return path, row

    def _local_scope(self, step, receipt):
        if any(path.is_symlink() for path in (step, *step.parents)):
            raise ValueError("Local recovery path cannot contain symlinks")
        run_identity = session_identity = None
        for parent in step.parents:
            run_path = parent / "integration-identity.json"
            session_path = parent / "session-identity.json"
            if run_identity is None and run_path.is_file():
                run_identity = run_path
            if session_identity is None and session_path.is_file():
                session_identity = session_path
        if run_identity is None or session_identity is None:
            raise ValueError("Local step requires complete run and session identities")
        _sha(run_identity)
        _sha(session_identity)
        run = json.loads(run_identity.read_text())
        session = json.loads(session_identity.read_text())
        if (
            run.get("run_id") != receipt.get("run_id")
            or session.get("run_id") != receipt.get("run_id")
            or not receipt.get("session")
            or session.get("session") != receipt.get("session")
            or session.get("role") != receipt.get("role")
            or not session_identity.parent.is_relative_to(run_identity.parent)
        ):
            raise ValueError("Local run/session does not bind shared request")
        run_root = run_identity.parent
        if any(path.is_symlink() for path in run_root.rglob("*")):
            raise ValueError("Complete run evidence cannot contain symlinks")
        for candidate in run_root.rglob("steps/*.json"):
            if _step_state(candidate) == ("tool", "submitted"):
                raise ValueError("Unknown tool in complete run forbids model disposition")
        matching = []
        for candidate in session_identity.parent.rglob("steps/*.json"):
            item = json.loads(candidate.read_text())
            if item.get("kind") != "model" or item.get("state") != "submitted":
                continue
            effective = dict(
                item["input"],
                temperature=0,
                durable=True,
                use_cache=False,
                max_tokens=receipt["max_tokens"],
            )
            if digest(effective) == receipt["request_fingerprint"]:
                matching.append(candidate.resolve())
        if matching != [step.resolve()]:
            raise ValueError("Unknown request requires a unique original session step")
        return {
            str(run_identity): _sha(run_identity),
            str(session_identity): _sha(session_identity),
        }

    def _validate_snapshot(self, receipt, step):
        path, row = self._receipt(receipt)
        step = Path(step)
        _sha(step)
        local = json.loads(step.read_text())
        if row["status"] != "unknown" or not row.get("finished"):
            raise ValueError("Only a stopped unknown request can be acknowledged")
        if local.get("kind") != "model" or local.get("state") != "submitted" or "response" in local:
            raise ValueError("Only an unresolved model step can be acknowledged")
        inp = local["input"]
        if inp.get("role") != row.get("role") or inp.get("namespace") != row.get("namespace"):
            raise ValueError("Local step and receipt role mismatch")
        effective = dict(
            inp, temperature=0, durable=True, use_cache=False, max_tokens=row["max_tokens"]
        )
        if digest(effective) != row["request_fingerprint"]:
            raise ValueError("Local step does not bind the dispatched model request")
        identities = self._local_scope(step, row)
        policy_path = self.root / "policy.json"
        _sha(policy_path)
        policy = json.loads(policy_path.read_text())
        if Path(policy["http_ledger"]).resolve() != self.ledger:
            raise ValueError("Recovery must reuse original HTTP ledger")
        _sha(self.ledger)
        count = json.loads(self.ledger.read_text())["attempts"]
        records = []
        for candidate in (self.root / "requests").glob("*.json"):
            _, item = self._receipt(candidate)
            records.append(item)
        if any(item["status"] == "submitted" for item in records):
            raise ValueError("In-flight request forbids recovery")
        if count != sum(item.get("actual_http_attempts", 0) for item in records):
            raise ValueError("HTTP ledger and receipt attempts do not reconcile")
        if type(row.get("actual_http_attempts")) is not int or row["actual_http_attempts"] < 1:
            raise ValueError("Unknown dispatch cost must remain occupied")
        for candidate in step.parent.glob("*.json"):
            item = json.loads(candidate.read_text())
            if item.get("kind") == "tool" and item.get("state") == "submitted":
                raise ValueError("Unknown tool operation cannot be cleared by model disposition")
        return path, row, step, count, identities

    def record(self, receipt, step, *, authorization):
        if not isinstance(authorization, str) or not authorization.strip():
            raise ValueError("Explicit human authorization evidence is required")
        self._safe_folder()
        with execution_lease(self.root / "model-disposition.lock"):
            path, row, step, count, identities = self._validate_snapshot(receipt, step)
            pause = self.root / "active-pause.json"
            if not pause.exists() or json.loads(pause.read_text()).get("status") != "unknown":
                raise ValueError("An exact active unknown pause is required")
            target = self.folder / (row["id"] + ".json")
            if target.exists():
                raise ValueError("This request already has a disposition")
            value = {
                "schema": 1,
                "disposition": "unknown_abandoned",
                "request_id": row["id"],
                "run_id": row["run_id"],
                "receipt_sha256": _sha(path),
                "step": str(step.resolve()),
                "step_sha256": _sha(step),
                "policy_sha256": _sha(self.root / "policy.json"),
                "pause_sha256": _sha(pause),
                "http_attempts": count,
                "authorization": authorization,
                "local_identity_files": identities,
            }
            atomic_json(target, value)
            return target

    def acknowledged(self, receipt):
        path, row = self._receipt(receipt)
        target = self.folder / (row["id"] + ".json")
        if not target.exists():
            return False
        _sha(target)
        audit = json.loads(target.read_text())
        if audit.get("schema") != 1 or audit.get("disposition") != "unknown_abandoned":
            raise ValueError("Invalid model disposition schema")
        if not isinstance(audit.get("authorization"), str) or not audit["authorization"].strip():
            raise ValueError("Missing explicit authorization")
        path, row, step, count, identities = self._validate_snapshot(path, audit["step"])
        if (
            audit["request_id"] != row["id"]
            or audit["run_id"] != row["run_id"]
            or audit["receipt_sha256"] != _sha(path)
            or audit["step_sha256"] != _sha(step)
            or audit["policy_sha256"] != _sha(self.root / "policy.json")
            or count < audit["http_attempts"]
            or identities != audit.get("local_identity_files")
        ):
            raise ValueError("Model recovery evidence changed or ledger reset")
        return True

    def pause_acknowledged(self, pause):
        self._safe_folder()
        pause = Path(pause)
        if pause.resolve() != self.root / "active-pause.json" or not pause.exists():
            return False
        checksum = _sha(pause)
        if json.loads(pause.read_text()).get("status") != "unknown":
            return False
        for target in self.folder.glob("*.json"):
            _sha(target)
            audit = json.loads(target.read_text())
            if audit.get("pause_sha256") == checksum and self.acknowledged(
                self.root / "requests" / target.name
            ):
                return True
        return False
