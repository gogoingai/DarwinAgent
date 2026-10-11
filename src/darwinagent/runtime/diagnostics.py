"""Allowlisted failure evidence: never retain exception text or request bodies."""

import hashlib


def exception_evidence(error, *, stage="model"):
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    result = {"stage": stage, "error_type": type(error).__name__, "cause_types": chain}
    status = getattr(error, "status_code", None)
    if type(status) is int:
        result["http_status"] = status
    # No arbitrary strings from SDK exceptions: IDs may themselves echo secrets.
    diagnostics = getattr(error, "transport_diagnostics", {})
    if diagnostics.get("stage") in {"request", "stream", "stream_close"}:
        result["stage"] = diagnostics["stage"]
    for key in ("stream_complete", "chunks_received", "partial_content_chars", "reasoning_chars"):
        value = diagnostics.get(key)
        if type(value) in (bool, int):
            result[key] = value
    identifier = diagnostics.get("response_id")
    if isinstance(identifier, str):
        result["response_id_sha256"] = hashlib.sha256(identifier.encode()).hexdigest()
    return result
