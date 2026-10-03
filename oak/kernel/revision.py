"""Structured proposals, independent candidates and atomic version publication."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from oak.runtime.artifacts import atomic_json
from .assets import Asset, KernelAssets, KernelBundle
from .validation import validate_bundle


def training_id(case_id, question_id):
    """Unambiguous composite identity '<len(case_id)>:<case_id>::<question_id>': the length
    prefix makes '::' inside either id impossible to confuse with the delimiter."""
    return f'{len(case_id)}:{case_id}::{question_id}'


def parse_training_id(tid):
    """Inverse of training_id: recovers the exact (case_id, question_id) pair, or raises
    on malformed or ambiguous identities."""
    head, sep, rest = tid.partition(':')
    if not sep or not head.isdigit() or not head.isascii():
        raise ValueError(f'训练身份不可解析（应为 <len>:<case>::<question>）: {tid!r}')
    n = int(head)
    case_id, marker, question_id = rest[:n], rest[n:n + 2], rest[n + 2:]
    if len(case_id) != n or marker != '::' or not question_id:
        raise ValueError(f'训练身份不可解析（应为 <len>:<case>::<question>）: {tid!r}')
    return case_id, question_id


@dataclass(frozen=True)
class AssetPatch:
    asset: Asset
    base_fingerprint: str | None
    reason: str
    training_evidence: tuple[str, ...]

    def __post_init__(self):
        if not self.reason.strip() or not self.training_evidence:
            raise ValueError('Proposal requires a reason and current training evidence')

    def to_dict(self):
        return {'asset':self.asset.to_dict(),'base_fingerprint':self.base_fingerprint,
                'reason':self.reason,'training_evidence':list(self.training_evidence)}


class AssetRevisionService:
    def propose(self,base,patches,target: Path,training_ids,forbidden_questions=()):
        base.verify()
        target=Path(target)
        if target.exists(): raise ValueError('Candidate version already exists')
        if not patches or len({p.asset.id for p in patches})!=len(patches): raise ValueError('Empty or duplicate patch')
        assets={a.id:a for a in base.assets.assets}
        for p in patches:
            # Evidence must decode to a real (case, question) pair before it can count:
            # bare or ambiguous ids ('q1', 'a::b::q' without a length prefix) are rejected here.
            for tid in p.training_evidence:
                parse_training_id(tid)
            if set(p.training_evidence)-set(training_ids): raise ValueError('Proposal cites non-training evidence')
            existing=assets.get(p.asset.id)
            if existing:
                if p.base_fingerprint!=existing.fingerprint or p.asset.kind!=existing.kind:
                    raise ValueError('Stale baseline or asset type change')
            elif p.base_fingerprint is not None:
                raise ValueError('Unknown patch baseline')
            assets[p.asset.id]=p.asset
        # The graph mode is frozen at initialization: an S patch may extend the anchored
        # vocabulary but must not add, remove or alter the meta.anchoring declaration.
        from oak.schema.model import Schema
        base_anchor=Schema.from_yaml(next(a.content for a in base.assets.assets if a.kind=='S')).meta.get('anchoring')
        cand_anchor=Schema.from_yaml(next(a.content for a in assets.values() if a.kind=='S')).meta.get('anchoring')
        if base_anchor!=cand_anchor:
            raise ValueError('meta.anchoring 在初始化后冻结：不可通过补丁增删或改动（不可切换抽取模式）')
        target.parent.mkdir(parents=True,exist_ok=True)
        staging=Path(tempfile.mkdtemp(prefix='.candidate-',dir=target.parent))
        try:
            bundle=KernelAssets(tuple(assets.values()),{'kind':'proposal','base_version':base.version,
                'patches':[p.to_dict() for p in patches]}).export(staging/'bundle')
            validate_bundle(bundle,forbidden_questions)
            atomic_json(staging/'proposal.json',{'base_version':base.version,'candidate_version':bundle.version,
                                                'patches':[p.to_dict() for p in patches]})
            os.replace(staging,target)
        finally:
            if staging.exists(): shutil.rmtree(staging)
        return KernelBundle(target/'bundle')

    def publish(self,bundle,root: Path,decision):
        if decision.get('accepted') is not True: raise ValueError('Publication requires an accepted frozen-policy decision')
        bundle.verify()
        root=Path(root); root.mkdir(parents=True,exist_ok=True)
        version_root=root/'versions'/bundle.version
        if not version_root.exists():
            version_root.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix='.publish-',dir=version_root.parent))
            try:
                relocated=bundle.assets.export(staging/'bundle')
                validate_bundle(relocated)
                os.replace(staging/'bundle',version_root)
            finally: shutil.rmtree(staging)
        atomic_json(root/'current.json',{'version':bundle.version,'path':str(version_root.relative_to(root)),
                                         'decision':decision})
        return KernelBundle(version_root)
