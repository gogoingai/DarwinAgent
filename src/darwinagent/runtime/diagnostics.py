"""Allowlisted failure evidence: never retain exception text or request bodies."""

import hashlib

from openai import APIStatusError

_PROVIDER_ERROR_CATEGORIES = {
    "context_length_exceeded": "context_limit",
    "unsupported_parameter": "unsupported_parameter",
    "rate_limit_exceeded": "rate_limit",
    "invalid_api_key": "authentication",
    "model_not_found": "model_not_found",
}


# MiniMax's documented numeric codes: retain only fixed classifications.
# https://platform.minimax.cn/docs/api-reference/errorcode
_MINIMAX_ERROR_CATEGORIES = {
    "1002": "rate_limit",
    "1004": "authentication",
    "1008": "insufficient_balance",
    "1039": "token_limit",
    "2013": "invalid_parameters",
    "2049": "authentication",
    "2056": "resource_limit",
}
_ALLOWED_CATEGORIES = set(_PROVIDER_ERROR_CATEGORIES.values()) | set(
    _MINIMAX_ERROR_CATEGORIES.values()
)
_CONTEXT_LIMIT_SIGNATURE = "invalid params, context window exceeds limit (2013)"


def _provider_error_category(error):
    """Recognize fixed codes/full signatures; never persist provider prose."""
    if not isinstance(error, APIStatusError) or type(error.body) is not dict:
        return None
    body = error.body
    containers = [body]
    for key in ("error", "base_resp"):
        if key in body:
            if type(body[key]) is not dict:
                return None
            containers.append(body[key])
    codes, messages = [], []
    for item in containers:
        for key in ("code", "status_code"):
            if key in item:
                value = item[key]
                if type(value) not in (str, int):
                    return None
                code = str(value)
                if code not in _PROVIDER_ERROR_CATEGORIES and code not in _MINIMAX_ERROR_CATEGORIES:
                    return None
                codes.append(code)
        for key in ("message", "status_msg"):
            if type(item.get(key)) is str:
                messages.append(item[key])
    if len(set(codes)) > 1:
        return None
    context_signature = any(
        message.strip().casefold() == _CONTEXT_LIMIT_SIGNATURE for message in messages
    )
    if context_signature:
        # 2013 alone is a generic parameter error; this full signature is more specific.
        if codes and codes[0] not in {"2013", "context_length_exceeded"}:
            return None
        return "context_limit"
    if not codes:
        return None
    return _PROVIDER_ERROR_CATEGORIES.get(codes[0]) or _MINIMAX_ERROR_CATEGORIES.get(codes[0])


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
    category = _provider_error_category(error)
    if category is None:
        saved = getattr(error, "transport_diagnostics", {}).get("provider_error_category")
        if type(saved) is str and saved in _ALLOWED_CATEGORIES:
            category = saved
    if category is not None:
        result["provider_error_category"] = category
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
