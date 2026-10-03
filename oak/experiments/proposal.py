"""One model proposal from current training feedback, with no file editing capability."""
from __future__ import annotations

from oak.agents.protocol import ModelSession
from oak.kernel.assets import Asset
from oak.kernel.revision import AssetPatch, training_id
from oak.runtime.artifacts import atomic_json
from .bootstrap import revision_protocol


class ProposalGenerator:
    async def propose(self, base, cases, feedback, client, config, target, questions=None):
        if isinstance(cases, tuple) and len(cases) == 1:
            cases = cases[0]
        if not isinstance(cases, (list, tuple)):
            cases = (cases,)
        if questions is None:
            questions = [{"training_id": training_id(case.id, q.id), "text": q.text}
                         for case in cases for q in case.questions]
        session = ModelSession(client, config, 'proposal', limit=6)
        payload = {'base_version': base.version,
                   'assets': [dict(a.to_dict(), fingerprint=a.fingerprint) for a in base.assets.assets],
                   'task_training_feedback': feedback, 'questions': questions}
        # The protocol matches the bundle's real graph mode and carries patches as its only
        # output format; it is recorded for audit alongside the payload.
        protocol = revision_protocol(base)
        def valid(obj):
            if set(obj)!={'patches'} or not isinstance(obj['patches'],list) or not obj['patches']:
                raise ValueError('Expected a nonempty structured asset proposal')
            return tuple(AssetPatch(Asset(**p['asset']),p['base_fingerprint'],p['reason'],tuple(p['training_evidence'])) for p in obj['patches'])
        try:
            return await session.request(config.proposal_role,protocol,payload,valid,max_tokens=14000)
        finally:
            atomic_json(target,{'input':payload,'protocol':protocol,'raw_outputs':session.raw,'events':session.events})
