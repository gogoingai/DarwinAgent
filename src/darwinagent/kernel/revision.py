"""Structured proposals, independent candidates and atomic version publication."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json
from .assets import Asset, KernelAssets, KernelBundle
from .validation import validate_bundle


def training_id(case_id, question_id):
    """Unambiguous composite identity '<len(case_id)>:<case_id>::<question_id>': the length
    prefix makes '::' inside either id impossible to confuse with the delimiter."""
    return f"{len(case_id)}:{case_id}::{question_id}"


def parse_training_id(tid):
    """Inverse of training_id: recovers the exact (case_id, question_id) pair, or raises
    on malformed or ambiguous identities."""
    head, sep, rest = tid.partition(":")
    if not sep or not head.isdigit() or not head.isascii():
        raise ValueError(f"训练身份不可解析（应为 <len>:<case>::<question>）: {tid!r}")
    n = int(head)
    case_id, marker, question_id = rest[:n], rest[n : n + 2], rest[n + 2 :]
    if len(case_id) != n or marker != "::" or not question_id:
        raise ValueError(f"训练身份不可解析（应为 <len>:<case>::<question>）: {tid!r}")
    return case_id, question_id


@dataclass(frozen=True)
class AssetPatch:
    asset: Asset
    base_fingerprint: str | None
    reason: str
    training_evidence: tuple[str, ...]

    def __post_init__(self):
        if not self.reason.strip() or not self.training_evidence:
            raise ValueError("Proposal requires a reason and current training evidence")

    def to_dict(self):
        return {
            "asset": self.asset.to_dict(),
            "base_fingerprint": self.base_fingerprint,
            "reason": self.reason,
            "training_evidence": list(self.training_evidence),
        }


class AssetRevisionService:
    def propose(
        self,
        base,
        patches,
        target: Path,
        training_ids,
        forbidden_questions=(),
        allowed_kinds=(),
        required_capabilities=(),
    ):
        base.verify()
        target = Path(target)
        if target.exists():
            raise ValueError("Candidate version already exists")
        if not patches or len({p.asset.id for p in patches}) != len(patches):
            raise ValueError("Empty or duplicate patch")
        if allowed_kinds:
            outside = sorted({p.asset.kind for p in patches} - set(allowed_kinds))
            if outside:
                raise ValueError(
                    f"本轮迭代范围外资产类型: {outside}（允许: {sorted(set(allowed_kinds))}）"
                )
        assets = {a.id: a for a in base.assets.assets}
        for p in patches:
            # Evidence must decode to a real (case, question) pair before it can count:
            # bare or ambiguous ids ('q1', 'a::b::q' without a length prefix) are rejected here.
            for tid in p.training_evidence:
                parse_training_id(tid)
            if set(p.training_evidence) - set(training_ids):
                raise ValueError("Proposal cites non-training evidence")
            existing = assets.get(p.asset.id)
            if existing:
                if p.base_fingerprint != existing.fingerprint or p.asset.kind != existing.kind:
                    raise ValueError("Stale baseline or asset type change")
            elif p.base_fingerprint is not None:
                raise ValueError("Unknown patch baseline")
            assets[p.asset.id] = p.asset
        # The graph mode is frozen at initialization: an S patch may extend the anchored
        # vocabulary but must not add, remove or alter the meta.anchoring declaration.
        from darwinagent.schema.model import Schema

        base_s = next(a.content for a in base.assets.assets if a.kind == "S")
        cand_s = next(a.content for a in assets.values() if a.kind == "S")
        base_schema, cand_schema = Schema.from_yaml(base_s), Schema.from_yaml(cand_s)
        if base_schema.meta.get("anchoring") != cand_schema.meta.get("anchoring"):
            raise ValueError(
                "meta.anchoring 在初始化后冻结：不可通过补丁增删或改动（不可切换抽取模式）"
            )
        if base_schema.meta.get("atomic_memory_type"):
            # 原子记忆内核冻结：声明的类型名不可换，且换后的 S 必须仍满足硬下限。
            from .validation import atomic_memory_errors

            if cand_schema.meta.get("atomic_memory_type") != base_schema.meta.get(
                "atomic_memory_type"
            ):
                raise ValueError("meta.atomic_memory_type 在初始化后冻结：不可更换原子记忆节点类型")
            problems = atomic_memory_errors(cand_schema)
            if problems:
                raise ValueError("原子记忆内核不合规: " + str(problems))
        if required_capabilities:
            # 检索工具底线冻结（任务声明）：向量检索/关系遍历类 F 不可被迭代删光。
            from .validation import capability_floor_errors

            problems = capability_floor_errors(
                KernelAssets(tuple(assets.values())), required_capabilities
            )
            if problems:
                raise ValueError("检索工具底线违规: " + str(problems))
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=target.parent))
        try:
            bundle = KernelAssets(
                tuple(assets.values()),
                {
                    "kind": "proposal",
                    "base_version": base.version,
                    "patches": [p.to_dict() for p in patches],
                },
            ).export(staging / "bundle")
            validate_bundle(bundle, forbidden_questions)
            atomic_json(
                staging / "proposal.json",
                {
                    "base_version": base.version,
                    "candidate_version": bundle.version,
                    "patches": [p.to_dict() for p in patches],
                },
            )
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return KernelBundle(target / "bundle")

    def publish(self, bundle, root: Path, decision):
        if decision.get("accepted") is not True:
            raise ValueError("Publication requires an accepted frozen-policy decision")
        bundle.verify()
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        version_root = root / "versions" / bundle.version
        if not version_root.exists():
            version_root.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=version_root.parent))
            try:
                relocated = bundle.assets.export(staging / "bundle")
                validate_bundle(relocated)
                os.replace(staging / "bundle", version_root)
            finally:
                shutil.rmtree(staging)
        atomic_json(
            root / "current.json",
            {
                "version": bundle.version,
                "path": str(version_root.relative_to(root)),
                "decision": decision,
            },
        )
        return KernelBundle(version_root)
