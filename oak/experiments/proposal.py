"""One model proposal from current training feedback, with no file editing capability."""
from __future__ import annotations

from oak.agents.protocol import ModelSession
from oak.kernel.assets import Asset
from oak.kernel.revision import AssetPatch, training_id
from oak.runtime.artifacts import atomic_json
from .bootstrap import revision_protocol


class ProposalGenerator:
    async def propose(self, base, cases, feedback, client, config, target, questions=None,
                      allowed_kinds=(), admission_error=None):
        """allowed_kinds 与上一轮的准入错误都明示给提案模型：范围外补丁与指纹回显错
        应在模型侧重试消化，而不是整轮作废后重复同类错误。"""
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
                   'task_training_feedback': feedback, 'questions': questions,
                   'allowed_asset_kinds': sorted(set(allowed_kinds)) if allowed_kinds else []}
        if admission_error:
            payload['previous_admission_error'] = admission_error
        # output format; it is recorded for audit alongside the payload.
        protocol = revision_protocol(base, allowed_kinds)
        def valid(obj):
            if set(obj)!={'patches'} or not isinstance(obj['patches'],list) or not obj['patches']:
                raise ValueError('Expected a nonempty structured asset proposal')
            return tuple(AssetPatch(Asset(**p['asset']),p['base_fingerprint'],p['reason'],tuple(p['training_evidence'])) for p in obj['patches'])
        try:
            return await session.request(config.proposal_role,protocol,payload,valid,max_tokens=14000)
        finally:
            atomic_json(target,{'input':payload,'protocol':protocol,'raw_outputs':session.raw,'events':session.events})
