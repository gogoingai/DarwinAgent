"""Offline tests for durable proposal/Wiki dialogues."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.agents.protocol import ProtocolError
from darwinagent.experiments.proposal_session import ProposalSession, decode_action
from darwinagent.runtime.artifacts import atomic_json
from darwinagent.runtime.workspace import Workspace

CONFIG = SimpleNamespace(proposal_role="proposal", temperature=0, protocol_attempts=2)


class Client:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(next(self.replies)))


class Wiki:
    def __init__(self):
        self.queries = []

    async def query(self, query):
        self.queries.append(query)
        return {
            "status": "complete",
            "evidence_refs": ["full-original"],
            "facts": ["page did not advance"],
        }


class ProposalSessionTests(unittest.TestCase):
    def session(self, target, **kwargs):
        return ProposalSession(
            target, payload={"base_version": "b0"}, protocol="protocol", **kwargs
        )

    def test_format_feedback_contains_canonical_action_and_cursor_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            client = Client(
                [
                    {"query_wiki": {"query": {"question": "q3 pages?", "cursor": 0}}},
                    {"action": "no_change", "reason": "retain uncertainty"},
                ]
            )
            result = asyncio.run(self.session(target).run(client, CONFIG, decode_action))
            self.assertEqual(result["action"], "no_change")
            correction = client.calls[1]["messages"][-1]["content"]
            self.assertIn('"action":"query_wiki"', correction)
            self.assertIn('"cursor":null', correction)
            self.assertEqual(len(json.loads(target.read_text())["raw_outputs"]), 2)
            with self.assertRaisesRegex(ValueError, "cursor"):
                decode_action({"action": "query_wiki", "query": {"question": "q3", "cursor": 0}})

    def test_revised_protocol_resumes_bounded_failed_dialogue_without_losing_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            session = self.session(target)
            session.state.update(
                protocol="old incomplete protocol",
                format_failures=3,
                raw_outputs=["bad original 1", "bad original 2", "bad original 3"],
            )
            session.save()
            client = Client([{"action": "no_change", "reason": "repaired format"}])
            resumed = self.session(target)
            asyncio.run(resumed.run(client, CONFIG, decode_action))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(len(resumed.state["raw_outputs"]), 4)
            self.assertEqual(resumed.state["events"][0]["status"], "protocol_intervention")
            self.assertEqual(
                resumed.state["events"][0]["previous_protocol"], "old incomplete protocol"
            )

    def test_invalid_wiki_cursor_is_returned_to_dialogue_then_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            original = {"question": "q3 pages?", "scope": {"question_ids": ["q3"]}, "view": "raw"}

            class PagingWiki:
                def __init__(self):
                    self.calls = 0

                async def query(self, query):
                    self.calls += 1
                    if self.calls == 1:
                        return {"status": "partial", "cursor": "fixed-snapshot-page-2"}
                    if query.question != original["question"]:
                        raise ValueError("Reply cursor belongs to another query")
                    return {"status": "complete", "facts": ["remaining original evidence"]}

            client = Client(
                [
                    {"action": "query_wiki", "query": original},
                    {
                        "action": "query_wiki",
                        "query": {
                            **original,
                            "question": "changed question",
                            "cursor": "fixed-snapshot-page-2",
                        },
                    },
                    {
                        "action": "query_wiki",
                        "query": {**original, "cursor": "fixed-snapshot-page-2"},
                    },
                    {"action": "no_change", "reason": "completed original evidence"},
                ]
            )
            session = self.session(target)
            result = asyncio.run(session.run(client, CONFIG, decode_action, PagingWiki()))
            self.assertEqual(result["action"], "no_change")
            self.assertEqual(
                [ex["reply"]["status"] for ex in session.state["exchanges"]],
                ["partial", "failed", "complete"],
            )
            failed = session.state["exchanges"][1]["reply"]
            self.assertEqual(failed["retry_query"]["question"], original["question"])
            self.assertIn("continuation_query", client.calls[1]["messages"][-1]["content"])
            replay = Client([])
            asyncio.run(self.session(target).run(replay, CONFIG, decode_action))
            self.assertEqual(replay.calls, [])

    def test_query_then_terminal_and_resume_never_recalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            client = Client(
                [
                    {"action": "query_wiki", "query": {"question": "q3 pages?", "view": "regroup"}},
                    {"action": "no_change", "reason": "need more evidence"},
                ]
            )
            wiki = Wiki()
            result = asyncio.run(self.session(target).run(client, CONFIG, decode_action, wiki))
            self.assertEqual(result["action"], "no_change")
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(wiki.queries[0].view, "regroup")
            self.assertIn("full-original", client.calls[1]["messages"][-1]["content"])
            saved = json.loads(target.read_text())
            self.assertEqual(len(saved["exchanges"]), 1)
            replay = Client([])
            self.assertEqual(
                asyncio.run(self.session(target).run(replay, CONFIG, decode_action)), result
            )
            self.assertEqual(replay.calls, [])

    def test_unknown_request_blocks_auto_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            session = self.session(target)
            session.state.update(phase="submitted", request_id="unknown")
            session.save()
            client = Client([])
            with self.assertRaisesRegex(ProtocolError, "outcome unknown"):
                asyncio.run(self.session(target).run(client, CONFIG, decode_action))
            self.assertEqual(client.calls, [])

    def test_receipt_saved_before_state_recovers_without_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            session = self.session(target)
            session.state.update(phase="submitted", request_id="r1")
            session.save()
            atomic_json(
                target.with_name(target.name + ".receipt.json"),
                {"request_id": "r1", "content": '{"action":"no_change","reason":"done"}'},
            )
            client = Client([])
            result = asyncio.run(self.session(target).run(client, CONFIG, decode_action))
            self.assertEqual(result["reason"], "done")
            self.assertEqual(client.calls, [])

    def test_budget_is_adjustable_and_preserves_dialogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            with self.assertRaisesRegex(ProtocolError, "budget exhausted"):
                asyncio.run(
                    self.session(target, call_limit=0).run(Client([]), CONFIG, decode_action)
                )
            client = Client([{"action": "no_change", "reason": "enough"}])
            asyncio.run(self.session(target, call_limit=1).run(client, CONFIG, decode_action))
            self.assertEqual(len(client.calls), 1)

    def test_format_repair_is_bounded_and_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            client = Client([{"action": "invalid"}, {"action": "invalid"}])
            with self.assertRaises(ProtocolError):
                asyncio.run(self.session(target).run(client, CONFIG, decode_action))
            saved = json.loads(target.read_text())
            self.assertEqual(len(saved["raw_outputs"]), 2)
            self.assertEqual(len(client.calls), 2)

    def test_transport_unknown_is_registered_and_recovered_without_resending(self):
        class Broken(Client):
            async def chat(self, **kwargs):
                self.calls.append(kwargs)
                raise OSError("connection lost after submission")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            workspace = Workspace(Path(tmp) / "workspace")
            session = self.session(target, workspace=workspace)
            client = Broken([])
            with self.assertRaisesRegex(ProtocolError, "outcome unknown"):
                asyncio.run(session.run(client, CONFIG, decode_action))
            request_id = session.state["request_id"]
            self.assertEqual(workspace.request(request_id)["status"], "submitted")
            workspace.recover_requests()
            self.assertEqual(workspace.request(request_id)["status"], "unknown")
            session.recover_response('{"action":"no_change","reason":"recovered"}')
            replay = Client([])
            result = asyncio.run(session.run(replay, CONFIG, decode_action))
            self.assertEqual(result["reason"], "recovered")
            self.assertEqual(replay.calls, [])
            self.assertEqual(workspace.request(request_id)["status"], "responded")

    def test_baseline_change_requires_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            self.session(target)
            with self.assertRaisesRegex(ValueError, "baseline changed"):
                ProposalSession(target, payload={"base_version": "b1"}, protocol="protocol")

    def test_admission_feedback_retains_draft_and_allows_wiki_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            session = self.session(target)
            asyncio.run(
                session.run(
                    Client([{"patches": [{"draft": "invalid interface"}]}]), CONFIG, decode_action
                )
            )
            session.feedback("interface mismatch")
            wiki = Wiki()
            asyncio.run(
                session.run(
                    Client(
                        [
                            {
                                "action": "query_wiki",
                                "query": {"question": "what interface is required?"},
                            },
                            {"action": "no_change", "reason": "cannot repair safely"},
                        ]
                    ),
                    CONFIG,
                    decode_action,
                    wiki,
                )
            )
            saved = json.loads(target.read_text())
            self.assertIn("invalid interface", saved["raw_outputs"][0])
            self.assertTrue(any("interface mismatch" in m["content"] for m in saved["messages"]))
            self.assertEqual(len(wiki.queries), 1)

    def test_local_budget_rejection_remains_prepared_and_reuses_same_request(self):
        from darwinagent.llm.client import BudgetExceeded

        class Rejected:
            async def chat(self, **kwargs):
                self.durable = kwargs["durable"]
                raise BudgetExceeded("local cap")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            session = self.session(root / "proposal.json", workspace=workspace)
            client = Rejected()
            with self.assertRaises(BudgetExceeded):
                asyncio.run(session.run(client, CONFIG, decode_action))
            request_id = session.state["request_id"]
            self.assertTrue(client.durable)
            self.assertEqual(workspace.request(request_id)["status"], "prepared")
            self.assertEqual(session.state["calls"], 0)
            asyncio.run(
                session.run(
                    Client([{"action": "no_change", "reason": "done"}]), CONFIG, decode_action
                )
            )
            self.assertEqual(session.state["request_id"], request_id)
            self.assertEqual(workspace.request(request_id)["status"], "responded")
