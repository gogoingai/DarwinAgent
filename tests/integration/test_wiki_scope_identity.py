"""Qualified training scopes preserve case/question pairs and expose empty coverage."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from darwinagent.config import RunConfig
from darwinagent.experiments.proposal import ProposalGenerator
from darwinagent.experiments.proposal_session import ACTION_GUIDANCE
from darwinagent.experiments.wiki_service import WikiQuery, WikiService
from darwinagent.kernel.revision import training_id


class FakeWikiClient:
    def __init__(self):
        self.calls = []

    async def chat(self, **request):
        self.calls.append(request)
        evidence = json.loads(request["messages"][1]["content"])["evidence"]
        refs = [evidence["ref"]] if "ref" in evidence else evidence["refs"]
        return SimpleNamespace(
            content=json.dumps({"facts": [{"evidence_refs": refs, "text": "checked originals"}]})
        )


class WikiScopeIdentityTests(unittest.IsolatedAsyncioTestCase):
    def register_grid(self, service):
        return {
            (case, question): service.register(
                {"text": f"original {case}/{question}"},
                scope={
                    "case_ids": [case],
                    "question_ids": [question],
                    "asset_ids": ["lookup"],
                },
            )
            for case in ("train-a", "train-b")
            for question in ("q1", "q2")
        }

    async def test_multi_pair_object_case_and_question_filters_require_same_pair(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            ref = service.register(
                {"text": "paired original"},
                scope={
                    "training_ids": [training_id("train-a", "q1"), training_id("train-b", "q2")]
                },
            )
            crossed = await service.query(
                WikiQuery("crossed", {"case_ids": ["train-a"], "question_ids": ["q2"]})
            )
            self.assertEqual(crossed.evidence_refs, ())
            allowed = await service.query(
                WikiQuery("allowed", {"case_ids": ["train-a"], "question_ids": ["q1"]})
            )
            self.assertEqual(allowed.evidence_refs, (ref,))

    async def test_multi_pair_object_training_and_case_filters_require_same_pair(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            ref = service.register(
                {"text": "paired original"},
                scope={
                    "training_ids": [training_id("train-a", "q1"), training_id("train-b", "q2")]
                },
            )
            crossed = await service.query(
                WikiQuery(
                    "crossed",
                    {"training_ids": [training_id("train-a", "q1")], "case_ids": ["train-b"]},
                )
            )
            self.assertEqual(crossed.evidence_refs, ())
            allowed = await service.query(
                WikiQuery(
                    "allowed",
                    {"training_ids": [training_id("train-b", "q2")], "case_ids": ["train-b"]},
                )
            )
            self.assertEqual(allowed.evidence_refs, (ref,))

    async def test_legacy_ambiguous_ranges_do_not_invent_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            ambiguous = service.register(
                {"text": "range without pair mapping"},
                scope={"case_ids": ["train-a", "train-b"], "question_ids": ["q1", "q2"]},
            )
            for scope in (
                {"training_ids": [training_id("train-a", "q1")]},
                {"question_ids": [training_id("train-a", "q1")]},
                {"case_ids": ["train-a"], "question_ids": ["q1"]},
            ):
                reply = await service.query(WikiQuery("ambiguous", scope))
                self.assertFalse(reply.evidence_refs)
            case_only = await service.query(WikiQuery("range", {"case_ids": ["train-a"]}))
            self.assertEqual(case_only.evidence_refs, (ambiguous,))
            question_only = await service.query(WikiQuery("range", {"question_ids": ["q1"]}))
            self.assertEqual(question_only.evidence_refs, (ambiguous,))
            single_case = service.register(
                {"text": "single case multi question"},
                scope={"case_ids": ["train-c"], "question_ids": ["q1", "q2"]},
            )
            for scope in (
                {"training_ids": [training_id("train-c", "q2")]},
                {"question_ids": [training_id("train-c", "q1")]},
                {"case_ids": ["train-c"], "question_ids": ["q2"]},
            ):
                reply = await service.query(WikiQuery("known pairs", scope))
                self.assertEqual(reply.evidence_refs, (single_case,))

    async def test_plain_case_question_scope_remains_successful_control(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            refs = self.register_grid(service)
            reply = await service.query(
                WikiQuery("control", {"case_ids": ["train-a"], "question_ids": ["q1"]})
            )
            self.assertEqual(reply.status, "complete")
            self.assertEqual(reply.evidence_refs, (refs[("train-a", "q1")],))

    async def test_real_r3_qualified_question_scope_regroups_existing_originals(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeWikiClient()
            service = WikiService(root, lambda: client, RunConfig())
            refs = self.register_grid(service)
            reply = await service.query(
                WikiQuery(
                    "check all four training traces",
                    {"question_ids": [training_id(*pair) for pair in refs]},
                    view="regroup",
                )
            )
            self.assertEqual(reply.status, "complete")
            self.assertEqual(set(reply.evidence_refs), set(refs.values()))
            self.assertTrue(reply.covered)
            self.assertTrue(reply.matched)
            self.assertGreater(len(client.calls), 0)
            raw_requests = " ".join(call["messages"][1]["content"] for call in client.calls)
            for case, question in refs:
                self.assertIn(f"original {case}/{question}", raw_requests)

    async def test_canonical_and_qualified_scopes_never_cross_requested_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            refs = self.register_grid(service)
            pair_ids = [training_id("train-a", "q1"), training_id("train-b", "q2")]
            for key in ("training_ids", "question_ids"):
                reply = await service.query(WikiQuery("paired", {key: pair_ids}))
                self.assertEqual(
                    set(reply.evidence_refs),
                    {refs[("train-a", "q1")], refs[("train-b", "q2")]},
                )
            single = await service.query(
                WikiQuery("one", {"training_ids": [training_id("train-a", "q1")]})
            )
            self.assertEqual(single.evidence_refs, (refs[("train-a", "q1")],))
            filtered = await service.query(
                WikiQuery("case filter", {"training_ids": pair_ids, "case_ids": ["train-b"]})
            )
            self.assertEqual(filtered.evidence_refs, (refs[("train-b", "q2")],))

    async def test_legacy_scopes_corrections_canonical_registration_and_acl(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            refs = self.register_grid(service)
            target = refs[("train-a", "q1")]
            correction = service.correct(target, "counterevidence")
            legacy = await service.query(
                WikiQuery("legacy", {"case_ids": ["train-a"], "question_ids": ["q1"]})
            )
            qualified = await service.query(
                WikiQuery("qualified", {"training_ids": [training_id("train-a", "q1")]})
            )
            self.assertEqual(set(legacy.evidence_refs), {target, correction})
            self.assertEqual(set(qualified.evidence_refs), {target, correction})
            canonical = service.register(
                {"text": "canonical-only"},
                scope={"training_ids": [training_id("train-c", "q1")]},
            )
            reply = await service.query(
                WikiQuery("canonical index", {"case_ids": ["train-c"], "question_ids": ["q1"]})
            )
            self.assertEqual(reply.evidence_refs, (canonical,))
            with self.assertRaises(PermissionError):
                service.register(
                    {"gold": "restricted"},
                    scope={"training_ids": [training_id("train-a", "q1")]},
                )
            with self.assertRaises(PermissionError):
                service.register({"text": "validation trace"}, source_kind="validation")
            with self.assertRaises(PermissionError):
                service.correct(target, "bad reference", source_refs=("unknown-provenance",))

    async def test_unmatched_regroup_is_partial_with_gap_and_without_model_attempt(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            client = FakeWikiClient()
            service = WikiService(root, lambda: client, RunConfig())
            self.register_grid(service)
            scope = {"training_ids": [training_id("absent", "q1")]}
            reply = await service.query(WikiQuery("missing", scope, view="regroup"))
            self.assertEqual(reply.status, "partial")
            self.assertFalse(reply.evidence_refs)
            self.assertFalse(reply.covered)
            self.assertFalse(reply.matched)
            self.assertEqual(reply.missing[0]["reason"], "no_matching_evidence")
            self.assertEqual(reply.uncovered[0]["requested_scope"], scope)
            self.assertEqual(reply.uncertainty[0]["reason"], "regroup_not_performed")
            self.assertEqual(client.calls, [])
            self.assertEqual(
                service.workspace.attempts(f"wiki:{reply.evidence_version}", "regroup"),
                [],
            )
            self.assertFalse((service.root / "jobs" / f"{reply.evidence_version}.json").exists())

    async def test_corrupt_original_has_readable_coverage_gap_not_completed_regroup(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            client = FakeWikiClient()
            service = WikiService(root, lambda: client, RunConfig())
            ref = service.register({"text": "original"}, scope={"question_ids": ["q1"]})
            (service.root / "objects" / f"{ref}.json").write_text("corrupt")
            reply = await service.query(
                WikiQuery("damaged", {"question_ids": ["q1"]}, view="regroup")
            )
            self.assertEqual(reply.status, "partial")
            self.assertIn("reason", reply.missing[-1])
            self.assertEqual(reply.missing[-1]["reason"], "no_readable_evidence")
            self.assertEqual(reply.missing[0]["ref"], ref)
            self.assertEqual(client.calls, [])

    async def test_malformed_qualified_scope_rejected_without_broad_fallback(self):
        for scope in (
            {"training_ids": ["q1"]},
            {"question_ids": ["7:train-broken::q1"]},
        ):
            with self.assertRaises(ValueError):
                WikiQuery("invalid", scope)

    async def test_proposer_input_provides_canonical_scope_without_mutating_questions(
        self,
    ):
        questions = [{"training_id": training_id("train-a", "q1"), "text": "question"}]
        base = SimpleNamespace(version="base", assets=SimpleNamespace(assets=[]))
        with tempfile.TemporaryDirectory() as root:
            with patch(
                "darwinagent.experiments.proposal.revision_protocol",
                return_value="protocol",
            ):
                with patch(
                    "darwinagent.experiments.proposal.ProposalSession.run",
                    new=AsyncMock(return_value=()),
                ):
                    await ProposalGenerator().propose(
                        base,
                        [],
                        {},
                        None,
                        RunConfig(),
                        Path(root) / "session.json",
                        questions=questions,
                    )
            payload = json.loads((Path(root) / "session.json").read_text())["input"]
            question = payload["questions"][0]
            self.assertIn("case_id", question)
            self.assertEqual(question["case_id"], "train-a")
            self.assertEqual(question["question_id"], "q1")
            self.assertEqual(question["wiki_scope"], {"training_ids": [question["training_id"]]})
            self.assertNotIn("wiki_scope", questions[0])
            self.assertIn('"training_ids"', ACTION_GUIDANCE)
            self.assertIn("wiki_scope", ACTION_GUIDANCE)
