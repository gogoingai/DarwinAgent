"""No paid proposer spin when a repeated Wiki query has made no progress."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.experiments.proposal_session import ProposalSession, decode_action

CONFIG = SimpleNamespace(proposal_role="proposal", temperature=0, protocol_attempts=2)
QUERY = {"question": "check merge", "view": "regroup", "cursor": None}
BLOCKED = {
    "status": "pending",
    "evidence_version": "snapshot-1",
    "job_id": "job-1",
    "covered": [0, 1],
    "uncovered": ["global_merge"],
    "facts": [{"regroup_fragment": "saved chunks"}],
    "uncertainty": [{"control_state": {"state": "failed", "error": "empty response"}}],
}


class Client:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(next(self.actions)))


class Wiki:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def query(self, query):
        self.calls.append(query)
        return next(self.replies)


class WikiProgressTests(unittest.TestCase):
    def test_identical_blocked_reply_pauses_and_restart_makes_no_calls(self):
        from darwinagent.experiments.control import ControlSignal

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            session = ProposalSession(target, payload={"base_version": "b0"}, protocol="protocol")
            client = Client([{"action": "query_wiki", "query": QUERY}] * 6)
            wiki = Wiki([BLOCKED] * 6)
            with self.assertRaises(ControlSignal):
                asyncio.run(session.run(client, CONFIG, decode_action, wiki))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(session.state["phase"], "wiki_paused")
            self.assertEqual(len(session.state["exchanges"]), 2)
            resumed = ProposalSession(target, payload={"base_version": "b0"}, protocol="protocol")
            replay, no_wiki = Client([]), Wiki([])
            with self.assertRaises(ControlSignal):
                asyncio.run(resumed.run(replay, CONFIG, decode_action, no_wiki))
            self.assertEqual(replay.calls, [])
            self.assertEqual(no_wiki.calls, [])

    def test_pending_reply_with_new_coverage_and_cursor_can_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ProposalSession(
                Path(tmp) / "s.json", payload={"base_version": "b0"}, protocol="p"
            )
            client = Client(
                [
                    {"action": "query_wiki", "query": QUERY},
                    {"action": "query_wiki", "query": QUERY},
                    {"action": "query_wiki", "query": {**QUERY, "cursor": "page-2"}},
                    {"action": "no_change", "reason": "inspected"},
                ]
            )
            wiki = Wiki(
                [
                    BLOCKED,
                    {**BLOCKED, "covered": [0, 1, 2], "cursor": "page-2"},
                    {**BLOCKED, "status": "complete", "uncovered": []},
                ]
            )
            result = asyncio.run(session.run(client, CONFIG, decode_action, wiki))
            self.assertEqual(result["action"], "no_change")
            self.assertEqual(len(client.calls), 4)

    def test_explicit_retry_rechecks_original_query_without_proposer_until_progress(self):
        from darwinagent.experiments.control import ControlSignal

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            session = ProposalSession(target, payload={"base_version": "b0"}, protocol="p")
            with self.assertRaises(ControlSignal):
                asyncio.run(
                    session.run(
                        Client([{"action": "query_wiki", "query": QUERY}] * 2),
                        CONFIG,
                        decode_action,
                        Wiki([BLOCKED] * 2),
                    )
                )
            session.retry_wiki("operator retried global merge")
            replay = Client([])
            with self.assertRaises(ControlSignal):
                asyncio.run(
                    session.run(
                        replay,
                        CONFIG,
                        decode_action,
                        Wiki(
                            [
                                {
                                    **BLOCKED,
                                    "uncertainty": [{"error": "same failure with new timestamp"}],
                                }
                            ]
                        ),
                    )
                )
            self.assertEqual(replay.calls, [])
            self.assertEqual(session.state["phase"], "wiki_paused")
            session.retry_wiki("merge repaired with saved groups")
            proposer = Client([{"action": "no_change", "reason": "inspected repaired merge"}])
            result = asyncio.run(
                session.run(
                    proposer,
                    CONFIG,
                    decode_action,
                    Wiki(
                        [
                            {
                                **BLOCKED,
                                "status": "complete",
                                "uncovered": [],
                                "facts": [{"merged": "new supported conclusion"}],
                            }
                        ]
                    ),
                )
            )
            self.assertEqual(len(proposer.calls), 1)
            self.assertEqual(result["reason"], "inspected repaired merge")
            self.assertEqual(session.state["input"]["base_version"], "b0")
            self.assertEqual(len(session.state["exchanges"]), 4)

    def test_distinct_snapshots_and_progress_have_no_fixed_query_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ProposalSession(
                Path(tmp) / "s.json", payload={"base_version": "b0"}, protocol="p"
            )
            client = Client(
                [{"action": "query_wiki", "query": QUERY}] * 8
                + [{"action": "no_change", "reason": "eight snapshots inspected"}]
            )
            wiki = Wiki(
                [
                    {
                        **BLOCKED,
                        "evidence_version": "snapshot-" + str(index),
                        "job_id": "job-" + str(index),
                    }
                    for index in range(8)
                ]
            )
            asyncio.run(session.run(client, CONFIG, decode_action, wiki))
            self.assertEqual(len(wiki.calls), 8)
            self.assertEqual(len(client.calls), 9)
