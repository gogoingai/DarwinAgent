"""Atomic candidate-asset revisions, with evaluation outside the patch scope."""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from oak.runtime import atomic_json, digest
from . import KINDS, KernelBundle, fp


@dataclass(frozen=True)
class Patch:
    kind: str
    asset_id: str
    base_digest: str
    content: str
    reason: str
    failure_ids: tuple[str, ...]


def propose(base: KernelBundle, patch: Patch, target: Path) -> KernelBundle:
    base.verify()
    if patch.kind not in KINDS or not patch.reason.strip() or not patch.failure_ids:
        raise ValueError("Patch needs a kernel target, reason, and training failure evidence")
    if patch.base_digest != base.manifest["digest"]:
        raise ValueError("Patch base mismatch")
    # First implementation supports one existing-asset modification per proposal.
    source = base.path(patch.kind, patch.asset_id)
    if target.exists():
        raise ValueError("Candidate destination exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    working = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        shutil.copytree(base.root, working, dirs_exist_ok=True)
        path = working / source.relative_to(base.root)
        if path.suffix == ".py":
            compile(patch.content, str(path), "exec")
        path.write_text(patch.content)
        value = json.loads((working / "manifest.json").read_text())
        value.pop("digest")
        value["version"] = value["version"] + "+candidate"
        for asset in value["assets"]:
            if asset["kind"] == patch.kind and asset["id"] == patch.asset_id:
                asset["fp"] = fp(path)
        atomic_json(working / "manifest.json", {**value, "digest": digest(value)})
        candidate = KernelBundle.load(working)
        candidate.schema()
        if any(a["kind"] == "harness" for a in candidate.manifest["assets"]):
            candidate.harness()
        atomic_json(working / "proposal.json", {"kind": patch.kind, "asset_id": patch.asset_id,
                    "base_digest": patch.base_digest, "reason": patch.reason, "failure_ids": patch.failure_ids})
        if target.exists():
            raise ValueError("Candidate destination was created concurrently")
        working.rename(target)
        return KernelBundle.load(target)
    except BaseException:
        shutil.rmtree(working, ignore_errors=True)
        raise
