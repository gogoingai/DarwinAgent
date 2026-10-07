"""Offline shared artifacts fixtures; no test-case dependencies."""

from pathlib import Path


def _bundle_dir(bundle):
    return (
        Path(bundle.__dict__.get("path", bundle.assets.__dict__.get("path", str(bundle))))
        if hasattr(bundle, "__dict__")
        else Path(str(bundle))
    )


async def _fake_dual_grade_batch(items, client, context, cache_dir):
    return [{"idx": q.idx, "status": "ok", "precise": True} for q, _, _ in items]
