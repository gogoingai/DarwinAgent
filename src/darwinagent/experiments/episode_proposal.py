"""Version 4 proposals use the existing durable dialogue and training Wiki."""

from __future__ import annotations

import copy

from darwinagent.agent import AgentKernelAssets
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.budgets import counter_transaction

from .proposal_session import ProposalSession

PROTOCOL = """Propose a generic Agent kernel using only the supplied training evidence.
Keep the host profile, primitive tools, model configuration and evaluator fixed.
Use query_wiki to inspect training originals. Never request validation/test references.
Use no_change when evidence is insufficient. To submit a full S/F/C/P candidate use
{"action":"submit_patch","patches":{"assets":[complete asset declarations]}}.
Alternatively patches is a list of {"op":"upsert","asset":{complete asset declaration}}
or {"op":"delete","id":"asset_id"}. All non-S assets depend on the single S.
F uses run(params), call and view; C uses check(candidate); P uses registered host slots.
Static validity is followed by isolated behavioral admission and paired validation.
"""


class WikiBudgetClient:
    """Charge all regroup/merge calls before dispatch, including across resume."""

    def __init__(self, factory, stage, counter_path, limit):
        self.factory, self.stage = factory, stage
        self.counter_path, self.limit = counter_path, limit

    async def chat(self, **kwargs):
        with counter_transaction(self.counter_path) as state:
            used = state.get("reserved_calls", 0)
            if used >= self.limit:
                raise BudgetExceeded("Generic Wiki model call budget exhausted", dispatched=False)
            state["reserved_calls"] = used + 1
        client = self.factory(self.stage)
        try:
            return await client.chat(**kwargs)
        finally:
            await client.aclose()

    async def aclose(self):
        pass


def decode_candidate(action, base, profile, origin):
    patches = action["patches"]
    if isinstance(patches, dict):
        payload = copy.deepcopy(patches)
    elif isinstance(patches, list):
        assets = {a.id: a.to_dict() for a in base.assets.assets}
        for patch in patches:
            if set(patch) == {"op", "asset"} and patch["op"] == "upsert":
                assets[patch["asset"]["id"]] = copy.deepcopy(patch["asset"])
            elif set(patch) == {"op", "id"} and patch["op"] == "delete":
                del assets[patch["id"]]
            else:
                raise ValueError("Expected an upsert or delete asset patch")
        payload = {"assets": list(assets.values())}
    else:
        raise ValueError("Expected v4 assets or a patch list")
    if not isinstance(payload.get("assets"), list) or any(
        not isinstance(row, dict) or not isinstance(row.get("content"), str)
        for row in payload["assets"]
    ):
        raise ValueError("Candidate assets must be declarations with text content")
    return AgentKernelAssets.from_payload(payload, profile, origin)


async def propose(owner, wiki, base, root, call_limit):
    payload = {
        "base_version": base.content_id,
        "assets": [a.to_dict() for a in base.assets.assets],
        "profile": base.profile_declaration,
        "training": wiki.context(),
    }
    session = ProposalSession(
        root / "proposal.json",
        payload=payload,
        protocol=PROTOCOL,
        call_limit=call_limit,
        workspace=wiki.service.workspace,
    )
    client = owner._client(root / "proposal-runtime")
    try:
        # Save every submitted candidate before static/behavioral admission. The
        # ProposalSession still validates action syntax and handles Wiki queries.
        return await session.run(client, owner.config, lambda action: action, wiki.service)
    finally:
        await client.aclose()
