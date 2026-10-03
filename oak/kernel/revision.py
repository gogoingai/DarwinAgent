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
            if set(p.training_evidence)-set(training_ids): raise ValueError('Proposal cites non-training evidence')
            existing=assets.get(p.asset.id)
            if existing:
                if p.base_fingerprint!=existing.fingerprint or p.asset.kind!=existing.kind:
                    raise ValueError('Stale baseline or asset type change')
            elif p.base_fingerprint is not None:
                raise ValueError('Unknown patch baseline')
            assets[p.asset.id]=p.asset
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
