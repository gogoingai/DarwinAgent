"""Fingerprint engineering code and transport policy, independently of asset versions."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .artifacts import digest, verify_files


def snapshot_files(paths):
    result = {}
    for item in paths:
        p = Path(item).resolve()
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for file in files:
            if file.is_file():
                result[str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def assert_files(snapshot):
    changed = [
        p
        for p, h in snapshot.items()
        if not Path(p).is_file() or hashlib.sha256(Path(p).read_bytes()).hexdigest() != h
    ]
    if changed:
        raise ValueError(f"Frozen engineering/input files changed: {changed}")


def transport_identity(client):
    cfg = getattr(client, "cfg", None)
    if cfg is None:
        return {"transport": type(client).__name__}
    fields = (
        "api_base_url",
        "fast_base_url",
        "middle_base_url",
        "model_strong",
        "model_fast",
        "model_middle",
        "role_tiers",
        "namespace_limits",
        "thinking_disabled_roles",
        "empty_response_passthrough_roles",
        "max_concurrency",
        "fast_max_concurrency",
        "max_retries",
        "reasoning_effort",
        "model_profiles",
        "request_timeout_s",
    )
    result = {}
    for field in fields:
        if hasattr(cfg, field):
            value = getattr(cfg, field)
            result[field] = sorted(value) if isinstance(value, set) else value
    # Detect credential changes without publishing credentials or reversible hints.
    result["credential_identity"] = digest(
        [getattr(cfg, "api_key", ""), getattr(cfg, "fast_api_key", "")]
    )
    # 注册表结构版本：模型参数/站点语义变化时随之失效（函数内导入避免环）
    from ..llm.registry import REGISTRY_VERSION

    result["registry_version"] = REGISTRY_VERSION
    return result
