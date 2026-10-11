"""Offline unlimited proposal, executable asset and lossless evidence contracts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.agent import AgentAsset, ExecutionProfile
from darwinagent.experiments.episode_contracts import AgentExperimentSpec
from darwinagent.experiments.episode_proposal import WikiBudgetClient, decode_candidate
from darwinagent.experiments.proposal_session import ProposalSession, decode_action
from darwinagent.experiments.wiki_service import WikiQuery, WikiService
from darwinagent.llm.client import BudgetExceeded
from tests.support.agent_runtime import RawRecordedClient, bundle

CONFIG = SimpleNamespace(proposal_role="proposal", temperature=0, protocol_attempts=2)


class Client(RawRecordedClient):
    def __init__(self, replies):
        super().__init__([json.dumps(reply) for reply in replies])

    @property
    def calls(self):
        return self.requests


class UnlimitedProposalTests(unittest.TestCase):
    def test_unlimited_spec_is_opt_in_and_declared(self):
        self.assertEqual(AgentExperimentSpec(("t",), ("v",), ("a",)).proposal_call_limit, 4)
        self.assertIsNone(
            AgentExperimentSpec(("t",), ("v",), ("a",), proposal_call_limit=None).declaration()[
                "proposal_call_limit"
            ]
        )
        for value in (0, -1, True, "unlimited"):
            with self.assertRaises(ValueError):
                AgentExperimentSpec(("t",), ("v",), ("a",), proposal_call_limit=value)

    def test_wiki_unlimited_counts_every_dispatch_and_finite_budget_still_stops(self):
        class Transport:
            calls = 0
            closed = 0

            async def chat(self, **kwargs):
                self.calls += 1
                return "reply"

            async def aclose(self):
                self.closed += 1

        with tempfile.TemporaryDirectory() as tmp:
            transport = Transport()
            counter = Path(tmp) / "counter.json"
            client = WikiBudgetClient(lambda stage: transport, "wiki", counter, None)
            for _ in range(9):
                asyncio.run(client.chat(messages=[]))
            self.assertEqual(json.loads(counter.read_text())["reserved_calls"], 9)
            self.assertEqual((transport.calls, transport.closed), (9, 9))
            with self.assertRaises(BudgetExceeded):
                asyncio.run(
                    WikiBudgetClient(lambda stage: transport, "wiki", counter, 9).chat(messages=[])
                )
            self.assertEqual(transport.calls, 9)

    def test_more_than_six_queries_then_real_sfcp_candidate_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = ExecutionProfile("files", ("read_file", "write_file"))
            base = bundle(root / "base", profile)
            patches = []
            for asset in base.assets.assets:
                declaration = asset.to_dict()
                if asset.kind == "P":
                    declaration["content"] = (
                        "Read the output requirements before writing. $instruction"
                    )
                patches.append({"op": "upsert", "asset": declaration})
            patches.append(
                {
                    "op": "upsert",
                    "asset": AgentAsset(
                        "guard",
                        "C",
                        "def check(candidate):\n    return True",
                        schema_dependencies=("schema",),
                        stage="final_delivery",
                    ).to_dict(),
                }
            )
            action = {"action": "submit_patch", "patches": patches}

            class Wiki:
                async def query(self, query):
                    return {"status": "complete", "facts": [query.question]}

            client = Client(
                [
                    {"action": "query_wiki", "query": {"question": "training fact " + str(i)}}
                    for i in range(8)
                ]
                + [action]
            )
            session = ProposalSession(
                root / "proposal.json",
                payload={"base_version": base.content_id},
                protocol="training only",
                call_limit=None,
            )

            def decoder(reply):
                if reply["action"] != "submit_patch":
                    return reply
                return decode_candidate(reply, base, profile, {})

            candidate = asyncio.run(session.run(client, CONFIG, decoder, Wiki()))
            self.assertEqual({a.kind for a in candidate.assets}, {"S", "F", "C", "P"})
            self.assertNotEqual(candidate.content_id, base.content_id)
            self.assertEqual((session.state["calls"], len(session.state["raw_outputs"])), (9, 9))
            self.assertEqual(len(session.state["exchanges"]), 8)
            replay = Client([])
            self.assertEqual(
                asyncio.run(session.run(replay, CONFIG, decoder)).content_id, candidate.content_id
            )
            self.assertEqual(replay.calls, [])
            invalid = {
                "action": "submit_patch",
                "patches": {
                    "assets": [
                        {**a.to_dict(), "stage": "private_validation"}
                        if a.kind == "C"
                        else a.to_dict()
                        for a in candidate.assets
                    ]
                },
            }
            with self.assertRaisesRegex(ValueError, "lifecycle"):
                decode_candidate(invalid, base, profile, {})

    def test_same_evidence_with_reworded_question_guides_without_hiding_query(self):
        with tempfile.TemporaryDirectory() as tmp:

            class Wiki:
                async def query(self, query):
                    return {"status": "complete", "facts": ["unchanged fact"]}

            session = ProposalSession(
                Path(tmp) / "s.json", payload={"base_version": "b"}, protocol="p"
            )
            client = Client(
                [
                    {"action": "query_wiki", "query": {"question": q}}
                    for q in ("first question", "reworded question")
                ]
                + [{"action": "no_change", "reason": "insufficient evidence"}]
            )
            asyncio.run(session.run(client, CONFIG, decode_action, Wiki()))
            feedback = json.loads(session.state["messages"][-2]["content"])
            self.assertEqual(feedback["wiki_feedback"]["status"], "no_new_evidence")
            self.assertEqual(len(session.state["exchanges"]), 2)
            self.assertEqual(session.state["calls"], 3)

    def test_reworded_raw_partial_query_reports_no_new_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WikiService(tmp)
            ref = service.register({"text": "training source " * 800})
            client = Client(
                [
                    {
                        "action": "query_wiki",
                        "query": {
                            "question": q,
                            "scope": {"evidence_refs": [ref]},
                            "max_chars": 1800,
                        },
                    }
                    for q in ("inspect failure", "reworded failure")
                ]
                + [{"action": "no_change", "reason": "insufficient evidence"}]
            )
            session = ProposalSession(
                Path(tmp) / "s.json", payload={"base_version": "b"}, protocol="p"
            )
            asyncio.run(session.run(client, CONFIG, decode_action, service))
            first, second = session.state["exchanges"]
            self.assertEqual(first["reply"]["status"], "partial")
            self.assertNotEqual(first["reply"]["cursor"], second["reply"]["cursor"])
            feedback = json.loads(session.state["messages"][-2]["content"])
            self.assertEqual(feedback["wiki_feedback"]["status"], "no_new_evidence")
            self.assertEqual(session.state["calls"], 3)

    def test_many_refs_preserve_full_source_chunk_in_outer_paging(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WikiService(tmp)
            for i in range(61):
                service.register({"index": i, "text": "source " * 30})
            query = {"question": "all training originals", "max_chars": 8000}
            reply = asyncio.run(service.query(WikiQuery(**query)))
            self.assertIn("reply_fragment", reply.facts[0])
            source_chunks = []
            while True:
                outer = []
                while True:
                    self.assertLessEqual(len(json.dumps(reply.to_dict(), ensure_ascii=False)), 8000)
                    outer.append(reply.facts[0]["reply_fragment"])
                    if not reply.cursor or "reply" not in json.loads(reply.cursor):
                        break
                    reply = asyncio.run(service.query(WikiQuery(**query, cursor=reply.cursor)))
                full = json.loads("".join(outer))
                fragment = full["facts"][0]["raw_fragment"]
                source_chunks.append(fragment)
                if len(source_chunks) == 1:
                    self.assertEqual(len(fragment), 7000)
                    self.assertEqual(json.loads(full["cursor"])["offset"], 7000)
                if not full["cursor"]:
                    break
                reply = asyncio.run(service.query(WikiQuery(**query, cursor=full["cursor"])))
            parts = json.loads("".join(source_chunks))
            originals = [json.loads(part["text"])["data"] for part in parts]
            self.assertEqual(originals, [{"index": i, "text": "source " * 30} for i in range(61)])

    def test_damaged_continuation_offers_exact_original_query_without_scope_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WikiService(tmp)
            ref = service.register({"text": "training evidence " * 800}, source_kind="training")
            original = {"question": "inspect", "scope": {"evidence_refs": [ref]}, "max_chars": 1800}

            class ClientWithDamagedCursor(Client):
                async def chat(self, **kwargs):
                    if len(self.requests) == 1:
                        continuation = json.loads(kwargs["messages"][-1]["content"])[
                            "continuation_query"
                        ]
                        self.responses[1] = json.dumps(
                            {
                                "action": "query_wiki",
                                "query": {
                                    **continuation,
                                    "scope": {"evidence_refs": ["broken hash"]},
                                    "cursor": "broken cursor",
                                },
                            }
                        )
                    return await super().chat(**kwargs)

            client = ClientWithDamagedCursor(
                [
                    {"action": "query_wiki", "query": original},
                    {},
                    {"action": "no_change", "reason": "repair explicitly deferred"},
                ]
            )
            session = ProposalSession(
                Path(tmp) / "s.json", payload={"base_version": "b"}, protocol="p"
            )
            asyncio.run(session.run(client, CONFIG, decode_action, service))
            exchanges = session.state["exchanges"]
            self.assertEqual(exchanges[1]["reply"]["status"], "failed")
            recovery = exchanges[1]["reply"]["retry_query"]
            self.assertEqual(recovery, {**original, "cursor": exchanges[0]["reply"]["cursor"]})
            self.assertEqual(exchanges[1]["query"]["scope"]["evidence_refs"], ["broken hash"])

    def test_raw_wire_paging_lossless_and_old_reply_cursor_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WikiService(tmp)
            source = {"training": ('quotes " and \\ slash\n中文 ' * 600), "tail": "final evidence"}
            ref = service.register(source, source_kind="training")
            query = {"question": "inspect", "scope": {"evidence_refs": [ref]}, "max_chars": 1800}
            chunks = []
            cursor = None
            while True:
                reply = asyncio.run(service.query(WikiQuery(**query, cursor=cursor))).to_dict()
                self.assertLessEqual(len(json.dumps(reply, ensure_ascii=False)), 1800)
                self.assertIn("raw_fragment", reply["facts"][0])
                chunks.append(reply["facts"][0]["raw_fragment"])
                cursor = reply["cursor"]
                if not cursor:
                    break
                with self.assertRaisesRegex(ValueError, "another query"):
                    asyncio.run(
                        service.query(WikiQuery(**{**query, "question": "changed"}, cursor=cursor))
                    )
            objects = json.loads("".join(chunks))
            original = json.loads("".join(row["text"] for row in objects))
            self.assertEqual(original["data"], source)
            # Persisted outer reply pages from old runs remain supported.
            from darwinagent.experiments.wiki_service import WikiReply
            from darwinagent.runtime.artifacts import digest

            legacy = WikiReply("complete", "snapshot", facts=({"text": "x" * 9000},))
            page = service._bound_reply(
                legacy,
                digest({"question": "inspect", "scope": query["scope"], "view": "raw"}),
                1800,
            )
            self.assertIn("reply_fragment", page.facts[0])
            continuation = asyncio.run(service.query(WikiQuery(**query, cursor=page.cursor)))
            self.assertGreater(continuation.facts[0]["offset"], 0)
            with self.assertRaisesRegex(ValueError, "another query"):
                asyncio.run(
                    service.query(WikiQuery(**{**query, "question": "changed"}, cursor=page.cursor))
                )
            with self.assertRaises(PermissionError):
                service.register({"private": "holdout"}, source_kind="validation")
