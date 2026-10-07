"""One model proposal from current training feedback, with no file editing capability."""

from __future__ import annotations

from darwinagent.agents.protocol import ModelSession
from darwinagent.kernel.assets import Asset
from darwinagent.kernel.revision import AssetPatch, training_id
from darwinagent.runtime.artifacts import atomic_json
from .bootstrap import revision_protocol


class ProposalGenerator:
    async def propose(
        self,
        base,
        cases,
        feedback,
        client,
        config,
        target,
        questions=None,
        allowed_kinds=(),
        admission_error=None,
        wiki_context=None,
    ):
        """allowed_kinds 与上一轮的准入错误都明示给提案模型：范围外补丁与指纹回显错
        应在模型侧重试消化，而不是整轮作废后重复同类错误。"""
        if isinstance(cases, tuple) and len(cases) == 1:
            cases = cases[0]
        if not isinstance(cases, (list, tuple)):
            cases = (cases,)
        if questions is None:
            questions = [
                {"training_id": training_id(case.id, q.id), "text": q.text}
                for case in cases
                for q in case.questions
            ]
        session = ModelSession(client, config, "proposal", limit=6)
        payload = {
            "base_version": base.version,
            "assets": [dict(a.to_dict(), fingerprint=a.fingerprint) for a in base.assets.assets],
            "questions": questions,
            "allowed_asset_kinds": sorted(set(allowed_kinds)) if allowed_kinds else [],
        }
        if wiki_context is None:
            payload["task_training_feedback"] = feedback
        else:
            payload["wiki"] = wiki_context
        if admission_error and wiki_context is None:
            payload["previous_admission_error"] = admission_error
        # output format; it is recorded for audit alongside the payload.
        protocol = revision_protocol(base, allowed_kinds)
        if wiki_context is not None:
            for asset in payload["assets"]:
                asset["current_ref"] = "current:" + asset["id"]
            protocol += (
                "\nWiki current-asset reference protocol overrides the hash-copy requirement: "
                "for an existing asset, set base_fingerprint to its current_ref (e.g. current:p_review). "
                "The framework resolves this reference against the frozen base_version and verifies the full fingerprint. "
                "Do not copy or edit the 64-character fingerprint. New assets still use null."
            )
        try:
            return await session.request(
                config.proposal_role,
                protocol,
                payload,
                lambda obj: self.decode(obj, base if wiki_context is not None else None),
                max_tokens=14000,
            )
        finally:
            atomic_json(
                target,
                {
                    "input": payload,
                    "protocol": protocol,
                    "raw_outputs": session.raw,
                    "events": session.events,
                },
            )

    @staticmethod
    def decode(obj, base=None):
        if set(obj) != {"patches"} or not isinstance(obj["patches"], list) or not obj["patches"]:
            raise ValueError("Expected a nonempty structured asset proposal")
        import dataclasses

        fields = {f.name for f in dataclasses.fields(Asset)}

        def _asset(item):
            extra = sorted(set(item) - fields - {"schema_dependencies"})
            if extra:
                item = {k: v for k, v in item.items() if k in fields or k == "schema_dependencies"}
                item["description"] = (
                    str(item.get("description", "")) + f" [normalize: dropped {extra}]"
                )
            return Asset(**item)

        current = {a.id: a for a in base.assets.assets} if base is not None else {}
        if base is not None:
            base.verify()
        patches = []
        for item in obj["patches"]:
            asset = _asset(item["asset"])
            fingerprint = item["base_fingerprint"]
            if isinstance(fingerprint, str) and fingerprint.startswith("current:"):
                if base is None or asset.id not in current or fingerprint != "current:" + asset.id:
                    raise ValueError("Invalid current-asset reference for " + asset.id)
                if asset.kind != current[asset.id].kind:
                    raise ValueError("Asset type change for " + asset.id)
                fingerprint = current[asset.id].fingerprint
            elif (
                base is not None
                and asset.id in current
                and fingerprint != current[asset.id].fingerprint
            ):
                raise ValueError("Wrong fingerprint for " + asset.id + "; use current:" + asset.id)
            patches.append(
                AssetPatch(asset, fingerprint, item["reason"], tuple(item["training_evidence"]))
            )
        return tuple(patches)
