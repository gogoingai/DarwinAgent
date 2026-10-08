"""One model proposal from current training feedback, with no file editing capability."""

from __future__ import annotations

from darwinagent.kernel.assets import Asset
from darwinagent.kernel.revision import AssetPatch, parse_training_id, training_id

from .bootstrap import revision_protocol
from .proposal_session import ProposalSession, decode_action


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
        wiki_service=None,
        call_limit=None,
        previous_session=None,
        objective=None,
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
        questions = [dict(question) for question in questions]
        for question in questions:
            if question.get("training_id"):
                case_id, question_id = parse_training_id(question["training_id"])
                question.update(
                    case_id=case_id,
                    question_id=question_id,
                    wiki_scope={"training_ids": [question["training_id"]]},
                )
        payload = {
            "base_version": base.version,
            "assets": [dict(a.to_dict(), fingerprint=a.fingerprint) for a in base.assets.assets],
            "questions": questions,
            "allowed_asset_kinds": sorted(set(allowed_kinds)) if allowed_kinds else [],
        }
        if objective is not None:
            payload["objective"] = {"direction": objective, "source": "human"}
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
        protocol += (
            '\nYou may return {"action":"query_wiki","query":{"question":"specific question",'
            '"scope":{},"view":"raw|summary|regroup","cursor":null,"max_chars":8000}} '
            "to obtain evidence and continue this same dialogue; "
            'or {"action":"submit_patch","patches":[...]} using the patch protocol; '
            'or {"action":"no_change","reason":"why","unresolved":[]}.'
        )
        session = ProposalSession(
            target,
            payload=payload,
            protocol=protocol,
            call_limit=call_limit,
            previous=previous_session,
            workspace=getattr(wiki_service, "workspace", None),
        )

        if admission_error and session.state.get("admission_feedback") != admission_error:
            session.feedback(admission_error)
            session.state["admission_feedback"] = admission_error
            session.save()

        def validate(action):
            if action["action"] == "submit_patch":
                return self.decode(
                    {"patches": action["patches"]}, base if wiki_context is not None else None
                )
            if action["action"] == "no_change":
                return ()
            return action

        return await session.run(client, config, validate, wiki_service)

    @staticmethod
    def decode_action(obj):
        return decode_action(obj)

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
