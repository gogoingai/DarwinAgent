"""One model proposal from current training feedback, with no file editing capability."""
from __future__ import annotations

from oak.agents.protocol import ModelSession
from oak.kernel.assets import Asset
from oak.kernel.revision import AssetPatch
from oak.runtime.artifacts import atomic_json
from .bootstrap import ASSET_PROTOCOL


class ProposalGenerator:
    async def propose(self,base,case,feedback,client,config,target):
        session=ModelSession(client,config,'proposal',limit=6)
        payload={'base_version':base.version,
                 'assets':[dict(a.to_dict(),fingerprint=a.fingerprint) for a in base.assets.assets],
                 'task_training_feedback':feedback,'questions':[{"training_id":q.id,"text":q.text} for q in case.questions]}
        protocol=ASSET_PROTOCOL+'''\nThis is one revision, not a fresh bootstrap. Return {"patches":[{"asset":a complete asset object,
"base_fingerprint":current asset fingerprint (null for a new asset),"reason":"diagnosis",
"training_evidence":[current training question ids]}]}. No paths, commands or framework changes.
S patches may only EXTEND the seed schema: additional entity classes in meta.entity_classes, additional
axioms or node/relation types. Dropping or altering the fact-anchoring vocabulary is rejected by admission.
Address generalizable causes in task assets. Scoring references are diagnostic only, never hardcoded generation answers.
'''
        def valid(obj):
            if set(obj)!={'patches'} or not isinstance(obj['patches'],list) or not obj['patches']:
                raise ValueError('Expected a nonempty structured asset proposal')
            return tuple(AssetPatch(Asset(**p['asset']),p['base_fingerprint'],p['reason'],tuple(p['training_evidence'])) for p in obj['patches'])
        try:
            return await session.request(config.proposal_role,protocol,payload,valid,max_tokens=14000)
        finally:
            atomic_json(target,{'input':payload,'raw_outputs':session.raw,'events':session.events})
