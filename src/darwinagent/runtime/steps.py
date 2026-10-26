"""Durable step replay. Unknown submissions require an explicit recovery choice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .artifacts import atomic_json, digest


class UnknownRequest(RuntimeError):
    """A submitted request has no durable receipt; resubmission is not implicit."""


class RequestAbandoned(RuntimeError):
    """Explicitly abandoned request remains historical and is not executed again."""


class AwaitingBudget(RuntimeError):
    """Request was rejected locally before HTTP dispatch; a budget grant can resume it."""


class StepJournal:
    def __init__(
        self, root, *, bypass_cache=False, workspace=None, provenance=None, create_root=True
    ):
        self.root = Path(root)
        if create_root:
            self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.provenance = provenance or {}
        self.position = 0
        self.bypass_cache = bypass_cache

    def reserve(self, kind, payload):
        key = f"{self.position:06d}"
        self.position += 1
        path = self.root / (key + ".json")
        semantic_input = payload
        if kind == "model":
            semantic_input = {
                key: value
                for key, value in payload.items()
                if key not in ("temperature", "max_tokens")
            }
        fingerprint = digest({"kind": kind, "input": semantic_input})
        if path.exists():
            row = json.loads(path.read_text())
            original_path = path
            path, row = self._authorized_retry(path, row)
            if path != original_path and row["input"] != payload:
                raise ValueError("Authorized model retry must preserve exact input")
            changed = row["fingerprint"] != fingerprint
            if changed:
                if row["kind"] != kind or row["input"].get("role") != payload.get("role"):
                    raise ValueError("Saved step protocol role changed; choose a new attempt")
                if self.workspace is not None:
                    self.workspace.append_event(
                        "saved_step_input_changed",
                        {
                            "journal": str(path),
                            "original_fingerprint": row["fingerprint"],
                            "current_input_ref": self.workspace.put_json(payload),
                            "reuse_preserves_original_source": True,
                        },
                    )
            if row["state"] == "responded":
                if digest(row["response"]) != row["response_digest"]:
                    raise ValueError("Saved step response changed")
                return path, row["response"]
            if self.workspace is not None and row.get("request_id"):
                replacement = self.workspace.replacement_for(row["request_id"])
                if replacement is not None:
                    row.setdefault("previous_requests", []).append(row["request_id"])
                    row["request_id"] = replacement["id"]
                    row["state"] = (
                        "prepared" if replacement["status"] == "prepared" else "submitted"
                    )
                    self.bypass_cache = True
                    atomic_json(path, row)
            if (
                self.workspace is not None
                and row.get("request_id")
                and row["state"] in ("submitted", "prepared")
            ):
                request = self.workspace.request(row["request_id"])
                receipt_path = self.workspace.receipts / (row["request_id"] + ".json")
                if receipt_path.exists():
                    self.workspace._accept_receipt(json.loads(receipt_path.read_text()))
                    request = self.workspace.request(row["request_id"])
                if request["status"] == "responded":
                    saved = self.workspace.read_json(request["response_ref"])
                    row.update(state="responded", response=saved, response_digest=digest(saved))
                    atomic_json(path, row)
                    return path, saved
                if request["status"] in ("submitted", "unknown"):
                    row["state"] = "submitted"
                elif request["status"] == "abandoned":
                    raise RequestAbandoned("Request explicitly abandoned")
                elif request["status"] == "failed":
                    raise RuntimeError(
                        f"Saved request {request['status']}; choose explicit new attempt"
                    )
            if row["state"] == "submitted":
                raise UnknownRequest(
                    f"Unknown request at {path}; recover, retry explicitly, or abandon"
                )
            if row["state"] == "failed":
                raise RuntimeError(f"Saved step failed: {row['error']}; choose failure retry")
            if row["state"] == "prepared":
                if changed:
                    row.setdefault("previous_inputs", []).append(
                        {
                            "input": row["input"],
                            "fingerprint": row["fingerprint"],
                            "request_id": row.get("request_id"),
                        }
                    )
                    if self.workspace is not None:
                        row["request_id"] = self.workspace.revise_prepared_request(
                            row["request_id"],
                            {"kind": kind, "input": payload, "provenance": self.provenance},
                        )
                    row.update(input=payload, fingerprint=fingerprint)
                    atomic_json(path, row)
                return path, None
        row = {"kind": kind, "fingerprint": fingerprint, "input": payload, "state": "prepared"}
        if self.workspace is not None:
            row["request_id"] = self.workspace.prepare_request(
                {"kind": kind, "input": payload, "provenance": self.provenance}
            )
        atomic_json(path, row)
        return path, None

    def read_active(self, path):
        """Resolve an authorized logical step without rewriting its history."""
        path = Path(path)
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise ValueError("Logical step must belong to this journal root")
        return self._authorized_retry(path, json.loads(path.read_text()))

    def _authorized_retry(self, path, row, seen=None):
        seen = set() if seen is None else seen
        resolved = path.resolve()
        if resolved in seen or len(seen) >= 100:
            raise ValueError("Cyclic or excessive model recovery chain")
        seen.add(resolved)
        folder = self.root / "model-retries"
        if folder.is_symlink():
            raise ValueError("Retry folder cannot be a symlink")
        audit = folder / (path.name + ".json")
        if audit.is_symlink():
            raise ValueError("Retry authorization cannot be a symlink")
        if not audit.exists():
            return path, row
        if self.workspace is not None or row["kind"] != "model":
            raise ValueError("Journal-only retry cannot bypass workspace or tool recovery")
        grant = json.loads(audit.read_text())
        if grant["original_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Original unknown journal changed")
        if not isinstance(grant.get("authorization"), str) or not grant["authorization"].strip():
            raise ValueError("Explicit authorization is required")
        retry = self.root / grant["retry_path"]
        if retry.is_symlink() or retry.resolve().parent != (self.root / "model-retries").resolve():
            raise ValueError("Invalid retry journal path")
        replacement = json.loads(retry.read_text())
        if replacement["kind"] != "model" or replacement["fingerprint"] != row["fingerprint"]:
            raise ValueError("Retry input changed")
        if replacement["input"] != row["input"]:
            raise ValueError("Retry must retain original model input")
        return self._authorized_retry(retry, replacement, seen)

    def retry_model(self, path, *, authorization):
        """Prepare an explicitly authorized model retry, retaining original bytes.

        Does not dispatch or acknowledge any shared transport gate. This method
        applies only to journals without a Workspace request database.
        """
        path = Path(path)
        if self.workspace is not None:
            raise ValueError("Use explicit Workspace request recovery")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Retry journal must belong to this journal root")
        if not isinstance(authorization, str) or not authorization.strip():
            raise ValueError("Explicit authorization is required")
        raw = path.read_bytes()
        row = json.loads(raw)
        if row["kind"] != "model" or row["state"] != "submitted" or "response" in row:
            raise ValueError("Only an unresolved model submission can be retried")
        folder = self.root / "model-retries"
        if folder.is_symlink():
            raise ValueError("Retry folder cannot be a symlink")
        folder.mkdir(exist_ok=True)
        audit = folder / (path.name + ".json")
        if audit.exists():
            raise ValueError("This model submission already has a recovery decision")
        sha = hashlib.sha256(raw).hexdigest()
        retry = folder / (path.stem + "-" + sha[:24] + ".json")
        if retry.exists():
            raise ValueError("Retry journal already exists")
        atomic_json(
            retry,
            {
                "kind": "model",
                "fingerprint": row["fingerprint"],
                "input": row["input"],
                "state": "prepared",
            },
        )
        atomic_json(
            audit,
            {
                "original_sha256": sha,
                "authorization": authorization,
                "retry_path": str(retry.relative_to(self.root)),
            },
        )
        return retry

    def submitted(self, path):
        row = json.loads(path.read_text())
        if self.workspace is not None:
            self.workspace.submit_request(row["request_id"])
        row["state"] = "submitted"
        atomic_json(path, row)

    def respond(self, path, response):
        row = json.loads(path.read_text())
        if self.workspace is not None:
            self.workspace.record_response(row["request_id"], response)
        row.update(state="responded", response=response, response_digest=digest(response))
        atomic_json(path, row)

    def unknown(self, path, exc):
        from .diagnostics import exception_evidence

        row = json.loads(path.read_text())
        row["diagnostics"] = exception_evidence(exc)
        atomic_json(path, row)

    def fail(self, path, exc):
        row = json.loads(path.read_text())
        if self.workspace is not None:
            self.workspace.set_request_status(row["request_id"], "failed")
        from .diagnostics import exception_evidence

        row.update(state="failed", error=type(exc).__name__, diagnostics=exception_evidence(exc))
        atomic_json(path, row)

    def tool(self, payload, operation):
        path, saved = self.reserve("tool", payload)
        if saved is not None or json.loads(path.read_text()).get("state") == "responded":
            return saved
        # Local read-only tools cannot incur an unknown external request.
        try:
            result = operation()
        except Exception as exc:
            self.fail(path, exc)
            raise
        self.respond(path, result)
        return result

    def save_state(self, state):
        value = {"position": self.position, **state}
        if self.workspace is not None:
            value["content_ref"] = self.workspace.put_json(state)
        atomic_json(self.root / "progress.json", value)

    def state(self):
        path = self.root / "progress.json"
        if not path.exists():
            raise ValueError("Missing saved stage input; select prerequisite explicitly")
        value = json.loads(path.read_text())
        if self.workspace is not None and value.get("content_ref"):
            saved = self.workspace.read_json(value["content_ref"])
            if any(value.get(k) != v for k, v in saved.items()):
                raise ValueError("Saved stage state changed")
        return value

    def awaiting_budget(self, path, exc):
        row = json.loads(path.read_text())
        if self.workspace is not None:
            self.workspace.defer_request(row["request_id"], reason=str(exc))
        row.update(state="prepared", pending_budget=str(exc))
        atomic_json(path, row)
