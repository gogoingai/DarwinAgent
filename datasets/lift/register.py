"""Explicit registration before native CLI/pipeline consumer imports; no upstream edits."""

import sys
from pathlib import Path


def register(factory):
    from src.lift.adapters import registry

    from src import models

    if getattr(registry, "_darwin_registered", False):
        raise RuntimeError("Darwin native registration already bound")
    # Consumer copies cannot be silently left stale.
    if "src.lift.pipeline.lift_pipeline" in sys.modules or "src.cli.lift_main" in sys.modules:
        raise RuntimeError("Register Darwin before importing native pipeline/CLI")
    original = registry.create_adapter

    def create(runtime, options):
        return (
            factory(options)
            if runtime.strip().lower() == "darwinagent"
            else original(runtime, options)
        )

    registry.SUPPORTED_RUNTIMES += ("darwinagent",)
    registry.create_adapter = create
    registry._darwin_registered = True
    models.LANGFUSE_PLUGIN_TRACE_NAMES += ("darwinagent-plugin",)
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("src.") and hasattr(
            module, "LANGFUSE_PLUGIN_TRACE_NAMES"
        ):
            module.LANGFUSE_PLUGIN_TRACE_NAMES = models.LANGFUSE_PLUGIN_TRACE_NAMES


def upstream(path):
    import subprocess

    from .data import UPSTREAM_REVISION

    path = Path(path).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"], text=True
    )
    if revision != UPSTREAM_REVISION or status:
        raise ValueError("Pinned upstream revision must be unchanged")
    sys.path.insert(0, str(path))
