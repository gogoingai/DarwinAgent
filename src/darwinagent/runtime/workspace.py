"""Durable content and execution records, independent of agents and datasets.

Callers must sanitize raw evidence before storage. Structured metadata rejects
credential fields; credentials belong only in the live transport configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class SelectionConflict(ValueError):
    """The selected branch changed while a result was being computed."""


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "api_key",
                "apikey",
                "password",
                "secret",
                "access_token",
                "authorization",
                "credential",
                "credentials",
            } or normalized.endswith(("_api_key", "_password", "_secret", "_access_token")):
                raise ValueError("Credentials must not enter workspace records")
            _safe(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _safe(child)
    return value


class Workspace:
    """An explicitly opened SQLite workspace with immutable content objects.

    Opening creates directories and schema, but never changes request states.
    ``recover_requests`` is an explicit startup recovery operation: invoke it
    only after the previous executor has stopped, not beside a live executor.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        self.receipts = self.root / "receipts"
        self.objects.mkdir(exist_ok=True)
        self.receipts.mkdir(exist_ok=True)
        self.database = self.root / "workspace.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    id TEXT PRIMARY KEY, format TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS provenance (
                    id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES objects(id),
                    metadata TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS refs (
                    owner TEXT NOT NULL, relation TEXT NOT NULL,
                    object_id TEXT NOT NULL REFERENCES objects(id),
                    PRIMARY KEY(owner, relation, object_id));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS branches (
                    name TEXT PRIMARY KEY, adopted TEXT, working TEXT,
                    revision INTEGER NOT NULL DEFAULT 0, parent TEXT);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, payload_ref TEXT NOT NULL REFERENCES objects(id),
                    status TEXT NOT NULL, response_ref TEXT REFERENCES objects(id),
                    receipt TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS request_replacements (
                    original_id TEXT PRIMARY KEY REFERENCES requests(id),
                    replacement_id TEXT NOT NULL UNIQUE REFERENCES requests(id));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL, stage TEXT NOT NULL,
                    status TEXT NOT NULL, progress TEXT NOT NULL,
                    result_ref TEXT REFERENCES objects(id), created REAL NOT NULL,
                    updated REAL NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _sync_directory(path):
        if os.name == "nt":
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def object_info(self, object_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise KeyError(object_id)
        return dict(row)

    def list_objects(self):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM objects ORDER BY id")]

    def object_path(self, object_id):
        if (
            not isinstance(object_id, str)
            or len(object_id) != 64
            or any(c not in "0123456789abcdef" for c in object_id)
        ):
            raise ValueError("Invalid content identifier")
        return self.objects / object_id[:2] / object_id[2:]

    def put_bytes(self, data: bytes, *, format="bytes"):
        if not isinstance(data, bytes) or not isinstance(format, str) or not format:
            raise ValueError("Expected bytes and a nonempty format")
        sha = hashlib.sha256(data).hexdigest()
        object_id = hashlib.sha256(format.encode() + b"\0" + data).hexdigest()
        path = self.object_path(object_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("Existing immutable object is corrupt")
        else:
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".object-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Exclusive link prevents concurrent writers overwriting an object.
                try:
                    os.link(temporary, path)
                    self._sync_directory(path.parent)
                except FileExistsError:
                    if path.read_bytes() != data:
                        raise ValueError("Existing immutable object is corrupt") from None
            finally:
                os.unlink(temporary)
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO objects VALUES (?,?,?,?,?)",
                (object_id, format, len(data), sha, time.time()),
            )
        return object_id

    def put_json(self, value):
        return self.put_bytes(_encode(_safe(value)).encode(), format="json")

    def read_bytes(self, object_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise KeyError(object_id)
        data = self.object_path(object_id).read_bytes()
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("Immutable object checksum mismatch")
        return data

    def read_json(self, object_id):
        return json.loads(self.read_bytes(object_id))

    def add_provenance(self, object_id, metadata):
        self.read_bytes(object_id)
        record_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute(
                "INSERT INTO provenance VALUES (?,?,?,?)",
                (record_id, object_id, _encode(_safe(metadata)), time.time()),
            )
        return record_id

    def provenance(self, object_id):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM provenance WHERE object_id=? ORDER BY created,id", (object_id,)
            ).fetchall()
        return [{**dict(r), "metadata": json.loads(r["metadata"])} for r in rows]

    def add_reference(self, owner, relation, object_id):
        self.read_bytes(object_id)
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO refs VALUES (?,?,?)", (owner, relation, object_id))

    def references(self, owner):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM refs WHERE owner=?", (owner,))]

    def append_event(self, kind, payload):
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO events(kind,payload,created) VALUES (?,?,?)",
                (kind, _encode(_safe(payload)), time.time()),
            )
            return cursor.lastrowid

    def events(self, *, after=0, kind=None):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE seq>? AND (? IS NULL OR kind=?) ORDER BY seq",
                (after, kind, kind),
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def create_branch(self, name, *, working=None, adopted=None, parent=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if parent is not None:
                source = db.execute("SELECT * FROM branches WHERE name=?", (parent,)).fetchone()
                if source is None:
                    raise KeyError(parent)
                working = source["working"] if working is None else working
                adopted = source["adopted"] if adopted is None else adopted
            db.execute(
                "INSERT INTO branches(name,adopted,working,parent) VALUES (?,?,?,?)",
                (name, adopted, working, parent),
            )
        return self.branch(name)

    def branch(self, name):
        with self._db() as db:
            row = db.execute("SELECT * FROM branches WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        return dict(row)

    def select_branch(
        self, name, *, expected_revision, working=None, adopted=None, source="automatic"
    ):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM branches WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(name)
            if row["revision"] != expected_revision:
                raise SelectionConflict("Branch selection changed; preserve detached result")
            values = (
                adopted if adopted is not None else row["adopted"],
                working if working is not None else row["working"],
            )
            db.execute(
                "UPDATE branches SET adopted=?,working=?,revision=revision+1 WHERE name=?",
                (*values, name),
            )
            db.execute(
                "INSERT INTO events(kind,payload,created) VALUES (?,?,?)",
                (
                    "selection",
                    _encode(
                        {
                            "branch": name,
                            "source": source,
                            "previous_revision": expected_revision,
                            "adopted": values[0],
                            "working": values[1],
                        }
                    ),
                    time.time(),
                ),
            )
        return self.branch(name)

    def publish_selection(
        self, name, *, expected_revision, working, adopted=None, publish=None, source="automatic"
    ):
        """Guard publication with the same transaction lock used by human selection.

        SQLite selection remains authoritative if the process dies after a file
        pointer was replaced but before the transaction committed. The caller
        retains a publication outbox so that pointer can be reconciled later.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM branches WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(name)
            if row["revision"] != expected_revision:
                raise SelectionConflict("Branch selection changed; preserve detached result")
            if publish is not None:
                publish()
            selected_adopted = row["adopted"] if adopted is None else adopted
            db.execute(
                "UPDATE branches SET adopted=?,working=?,revision=revision+1 WHERE name=?",
                (selected_adopted, working, name),
            )
            db.execute(
                "INSERT INTO events(kind,payload,created) VALUES (?,?,?)",
                (
                    "selection",
                    _encode(
                        {
                            "branch": name,
                            "source": source,
                            "previous_revision": expected_revision,
                            "adopted": selected_adopted,
                            "working": working,
                        }
                    ),
                    time.time(),
                ),
            )
        return self.branch(name)

    def prepare_request(self, payload, *, request_id=None):
        payload_ref = self.put_json(payload)
        request_id = request_id or uuid.uuid4().hex
        self._request_id(request_id)
        now = time.time()
        with self._db() as db:
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,NULL,NULL,?,?)",
                (request_id, payload_ref, "prepared", now, now),
            )
        return request_id

    @staticmethod
    def _request_id(request_id):
        if (
            not isinstance(request_id, str)
            or not request_id
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for c in request_id
            )
        ):
            raise ValueError("Invalid request identifier")

    def submit_request(self, request_id):
        with self._db() as db:
            cursor = db.execute(
                "UPDATE requests SET status='submitted',updated=? WHERE id=? AND status='prepared'",
                (time.time(), request_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a prepared request may be submitted")

    def write_receipt(self, request_id, response_ref, metadata=None):
        self._request_id(request_id)
        self.request(request_id)
        self.read_bytes(response_ref)
        receipt = {"request_id": request_id, "response_ref": response_ref, **_safe(metadata or {})}
        # Binding fields cannot be overridden by provider metadata.
        receipt["request_id"], receipt["response_ref"] = request_id, response_ref
        path = self.receipts / (request_id + ".json")
        if path.exists() and json.loads(path.read_text()) != receipt:
            raise ValueError("A different receipt already exists for this request")
        fd, temporary = tempfile.mkstemp(dir=self.receipts, prefix=".receipt-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_encode(receipt).encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                self._sync_directory(self.receipts)
            except FileExistsError:
                if json.loads(path.read_text()) != receipt:
                    raise ValueError(
                        "A different receipt already exists for this request"
                    ) from None
        finally:
            os.unlink(temporary)
        return receipt

    def record_response(self, request_id, response, *, metadata=None, format="json"):
        response_ref = (
            self.put_json(response) if format == "json" else self.put_bytes(response, format=format)
        )
        receipt = self.write_receipt(request_id, response_ref, metadata)
        self._accept_receipt(receipt)
        return response_ref

    def _accept_receipt(self, receipt):
        self.read_bytes(receipt["response_ref"])
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM requests WHERE id=?", (receipt["request_id"],)
            ).fetchone()
            if row is None:
                raise KeyError(receipt["request_id"])
            if row["response_ref"] not in (None, receipt["response_ref"]):
                raise ValueError("Conflicting response reference")
            db.execute(
                "UPDATE requests SET status='responded',response_ref=?,receipt=?,updated=? "
                "WHERE id=?",
                (receipt["response_ref"], _encode(receipt), time.time(), receipt["request_id"]),
            )

    def recover_requests(self):
        recovered = []
        for path in sorted(self.receipts.glob("*.json")):
            receipt = json.loads(path.read_text())
            if path.stem != receipt.get("request_id"):
                raise ValueError("Receipt request identifier mismatch")
            self._accept_receipt(receipt)
            recovered.append(receipt["request_id"])
        with self._db() as db:
            db.execute(
                "UPDATE requests SET status='unknown',updated=? WHERE status='submitted'",
                (time.time(),),
            )
        return recovered

    def request(self, request_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError(request_id)
        return {**dict(row), "receipt": json.loads(row["receipt"]) if row["receipt"] else None}

    def replacement_for(self, request_id):
        current, seen = request_id, set()
        with self._db() as db:
            while current not in seen:
                seen.add(current)
                row = db.execute(
                    "SELECT replacement_id FROM request_replacements WHERE original_id=?",
                    (current,),
                ).fetchone()
                if row is None:
                    return None if current == request_id else self.request(current)
                current = row["replacement_id"]
        raise ValueError("Request replacement cycle")

    def retry_request(self, request_id, *, reason="Explicit human retry of unknown request"):
        """Link a new paid attempt to the saved request; no transport is invoked."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Retry reason is required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current, seen = request_id, set()
            while current not in seen:
                seen.add(current)
                link = db.execute(
                    "SELECT replacement_id FROM request_replacements WHERE original_id=?",
                    (current,),
                ).fetchone()
                if link is None:
                    break
                current = link["replacement_id"]
            row = db.execute("SELECT * FROM requests WHERE id=?", (current,)).fetchone()
            if row is None:
                raise KeyError(current)
            if current != request_id and row["status"] == "prepared":
                return current
            if row["status"] not in ("unknown", "failed", "submitted", "abandoned"):
                raise ValueError("Only unresolved or failed requests may be retried")
            replacement, now = uuid.uuid4().hex, time.time()
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,NULL,NULL,?,?)",
                (replacement, row["payload_ref"], "prepared", now, now),
            )
            db.execute(
                "UPDATE requests SET status='abandoned',updated=? WHERE id=?", (now, current)
            )
            db.execute("INSERT INTO request_replacements VALUES (?,?)", (current, replacement))
            db.execute(
                "INSERT INTO events(kind,payload,created) VALUES (?,?,?)",
                (
                    "request_new_attempt",
                    _encode(
                        {
                            "previous": current,
                            "request": replacement,
                            "reason": reason,
                            "possible_duplicate_cost": True,
                        }
                    ),
                    now,
                ),
            )
        return replacement

    def set_request_status(self, request_id, status):
        if status not in {"failed", "unknown", "abandoned"}:
            raise ValueError("Use submit_request or record_response for successful transitions")
        with self._db() as db:
            cursor = db.execute(
                "UPDATE requests SET status=?,updated=? WHERE id=? AND status!='responded'",
                (status, time.time(), request_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Cannot discard a saved successful response")

    def start_attempt(self, task, stage, *, progress=None):
        attempt_id = uuid.uuid4().hex
        now = time.time()
        with self._db() as db:
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,NULL,?,?)",
                (attempt_id, task, stage, "running", _encode(_safe(progress or {})), now, now),
            )
        return attempt_id

    def save_progress(self, attempt_id, progress):
        with self._db() as db:
            cursor = db.execute(
                "UPDATE attempts SET progress=?,updated=? WHERE id=? AND status=?",
                (_encode(_safe(progress)), time.time(), attempt_id, "running"),
            )
            if cursor.rowcount != 1:
                raise ValueError("Progress belongs to an active attempt")

    def finish_attempt(self, attempt_id, status, *, result_ref=None):
        if status not in {"succeeded", "failed", "interrupted", "abandoned"}:
            raise ValueError("Invalid terminal attempt state")
        if result_ref is not None:
            self.read_bytes(result_ref)
        with self._db() as db:
            cursor = db.execute(
                "UPDATE attempts SET status=?,result_ref=?,updated=? WHERE id=? AND status=?",
                (status, result_ref, time.time(), attempt_id, "running"),
            )
            if cursor.rowcount != 1:
                raise ValueError("Attempt already finished or missing")

    def attempts(self, task, stage=None):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM attempts WHERE task=? AND (? IS NULL OR stage=?) "
                "ORDER BY created,id",
                (task, stage, stage),
            ).fetchall()
        return [{**dict(r), "progress": json.loads(r["progress"])} for r in rows]

    def defer_request(self, request_id, *, reason):
        """Only for a caller-confirmed local rejection before HTTP dispatch."""
        with self._db() as db:
            row = db.execute(
                "SELECT status,response_ref FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None or row["response_ref"] is not None:
                raise ValueError("Cannot defer a completed response")
            db.execute(
                "UPDATE requests SET status='prepared',updated=? WHERE id=?",
                (time.time(), request_id),
            )
        self.append_event(
            "request_awaiting_budget",
            {"request_id": request_id, "reason": reason, "dispatched": False},
        )

    def revise_prepared_request(self, request_id, payload):
        """Replace an undispatched request atomically, retaining its original input."""
        payload_ref = self.put_json(payload)
        replacement, now = uuid.uuid4().hex, time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "prepared":
                raise ValueError("Only an undispatched prepared request may change input")
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,NULL,NULL,?,?)",
                (replacement, payload_ref, "prepared", now, now),
            )
            db.execute(
                "UPDATE requests SET status='abandoned',updated=? WHERE id=?", (now, request_id)
            )
            db.execute("INSERT INTO request_replacements VALUES (?,?)", (request_id, replacement))
            db.execute(
                "INSERT INTO events(kind,payload,created) VALUES (?,?,?)",
                (
                    "request_input_revised",
                    _encode(
                        {
                            "previous": request_id,
                            "request": replacement,
                            "possible_duplicate_cost": False,
                        }
                    ),
                    now,
                ),
            )
        return replacement
