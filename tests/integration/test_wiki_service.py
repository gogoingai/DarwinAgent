"""Lossless Wiki queries, stable paging, regroup recovery and provenance guards."""

import json
import tempfile
import unittest
from types import SimpleNamespace

from darwinagent.experiments.wiki_context import _context_facts
from darwinagent.experiments.wiki_evidence import bounded_trace
from darwinagent.experiments.wiki_service import WikiQuery, WikiService


class WikiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_regroup_first_page_exposes_complete_cross_chunk_conclusion(self):
        from darwinagent.config import RunConfig

        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Connection:
                async def chat(self, **request):
                    calls.append(request)
                    evidence = json.loads(request["messages"][1]["content"])["evidence"]
                    if "refs" not in evidence:
                        finding = {
                            "facts": [{"ref": evidence["ref"], "text": "local detail " * 180}]
                        }
                    else:
                        finding = {
                            "facts": [
                                {"evidence_refs": evidence["refs"], "text": "cross-chunk-confirmed"}
                            ]
                        }
                    return SimpleNamespace(content=json.dumps(finding))

            connection = Connection()
            service = WikiService(root, client_factory=lambda: connection, config=RunConfig())
            service.register({"text": "original " * 3000})
            first = await service.query(WikiQuery("whole range", view="regroup", max_chars=4000))
            self.assertIsNotNone(first.cursor)
            self.assertIn("cross-chunk-confirmed", json.dumps(first.to_dict()))
            count = len(calls)
            await service.query(WikiQuery("whole range", view="regroup", max_chars=4000))
            self.assertEqual(len(calls), count)

    async def test_wrong_citation_field_gets_concrete_repair_schema(self):
        from darwinagent.config import RunConfig

        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Connection:
                async def chat(self, **request):
                    calls.append(request)
                    payload = json.loads(request["messages"][1]["content"])
                    ref = payload["evidence"]["ref"]
                    if len(calls) == 1:
                        return SimpleNamespace(
                            content=json.dumps({"facts": [{"cite": ref, "text": "fact"}]})
                        )
                    feedback = request["messages"][-1]["content"]
                    self_test.assertIn('"ref"', feedback)
                    self_test.assertIn(ref, feedback)
                    self_test.assertIn('"cite"', feedback)
                    return SimpleNamespace(
                        content=json.dumps({"facts": [{"ref": ref, "text": "fact"}]})
                    )

            self_test = self
            connection = Connection()
            service = WikiService(root, client_factory=lambda: connection, config=RunConfig())
            service.register({"text": "original"})
            reply = await service.query(WikiQuery("check", view="regroup"))
            self.assertEqual(reply.status, "complete")
            self.assertEqual(len(calls), 2)
            self.assertIn('"ref" fields', calls[0]["messages"][0]["content"])
            with self.assertRaisesRegex(ValueError, "allowed evidence"):
                service._validate({"facts": [{"ref": "other", "text": "unsupported"}]}, "allowed")

    async def test_explicit_wiki_completion_budget_reaches_chunk_and_merge(self):
        from darwinagent.config import RunConfig

        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Connection:
                async def chat(self, **request):
                    calls.append(request)
                    payload = json.loads(request["messages"][1]["content"])
                    evidence = payload["evidence"]
                    ref = evidence.get("ref") or evidence["refs"][0]
                    return SimpleNamespace(
                        content=json.dumps({"facts": [{"ref": ref, "text": "original supported"}]})
                    )

            connection = Connection()
            service = WikiService(
                root, client_factory=lambda: connection, config=RunConfig(wiki_max_tokens=12000)
            )
            service.register({"text": "original evidence " * 1200})
            reply = await service.query(WikiQuery("核对预算", view="regroup"))
            self.assertEqual(reply.status, "complete")
            self.assertGreater(len(calls), 1)
            self.assertTrue(all(call["max_tokens"] == 12000 for call in calls))

    async def test_raw_paging_fixed_snapshot_and_correction(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            ref = service.register({"text": "证据" * 12000}, scope={"question_ids": ["q3"]})
            first = await service.query(WikiQuery("原件", {"question_ids": ["q3"]}, max_chars=1000))
            service.correct(ref, "旧推断出现反证")

            async def unwrap(reply):
                fragments = ""
                while "reply_fragment" in reply.facts[0]:
                    fragments += reply.facts[0]["reply_fragment"]
                    try:
                        return json.loads(fragments)
                    except json.JSONDecodeError:
                        reply = await service.query(
                            WikiQuery(
                                "原件",
                                {"question_ids": ["q3"]},
                                cursor=reply.cursor,
                                max_chars=1000,
                            )
                        )
                return reply.to_dict()

            page = await unwrap(first)
            pieces = [page["facts"][0]["raw_fragment"]]
            while page["cursor"]:
                reply = await service.query(
                    WikiQuery(
                        "原件", {"question_ids": ["q3"]}, cursor=page["cursor"], max_chars=1000
                    )
                )
                self.assertEqual(reply.evidence_version, first.evidence_version)
                page = await unwrap(reply)
                pieces.append(page["facts"][0]["raw_fragment"])
            original = json.loads("".join(pieces))
            self.assertEqual(
                json.loads("".join(p["text"] for p in original))["data"]["text"], "证据" * 12000
            )
            latest = await service.query(WikiQuery("原件", {"question_ids": ["q3"]}))
            self.assertEqual(len(latest.evidence_refs), 2)

    async def test_regroup_reads_original_and_reuses_completed_chunks(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Client:
                async def chat(self, **kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(
                        content=json.dumps(
                            {
                                "facts": [
                                    {
                                        "ref": json.loads(kwargs["messages"][1]["content"])[
                                            "evidence"
                                        ]["ref"],
                                        "text": "checked",
                                    }
                                ]
                            }
                        )
                    )

            service = WikiService(
                root, Client, SimpleNamespace(protocol_attempts=1, temperature=0, max_tokens=1000)
            )
            ref = service.register({"text": "late original fact", "data": {"next_offset": 9}})
            reply = await service.query(WikiQuery("检查分页", view="regroup"))
            self.assertEqual(reply.status, "complete")
            self.assertIn("late original fact", calls[0]["messages"][1]["content"])
            self.assertIn(ref, calls[0]["messages"][1]["content"])
            await service.query(WikiQuery("检查分页", view="regroup"))
            self.assertEqual(len(calls), 1)

    async def test_missing_model_returns_raw_and_pending_job(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            service.register({"text": "evidence"})
            reply = await service.query(WikiQuery("重新归纳", view="regroup"))
            self.assertEqual(reply.status, "pending")
            self.assertTrue(reply.job_id)
            self.assertIn("evidence", reply.facts[0]["raw_fragment"])
            with self.assertRaises(PermissionError):
                service.register({"text": "gold"}, source_kind="gold")
            with self.assertRaises(PermissionError):
                service.register(
                    {"text": "correction"}, source_kind="correction", source_refs=["unknown"]
                )

    async def test_failed_chunk_explicit_retry_preserves_completed_and_merges(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Client:
                async def chat(self, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 2:
                        raise RuntimeError("offline injected failure")
                    payload = json.loads(kwargs["messages"][1]["content"])
                    return SimpleNamespace(
                        content=json.dumps(
                            {"facts": [{"ref": payload["evidence"]["ref"], "text": "checked"}]}
                        )
                    )

            service = WikiService(
                root, Client, SimpleNamespace(protocol_attempts=1, temperature=0, max_tokens=1000)
            )
            service.register({"text": "数据" * 10000})
            first = await service.query(WikiQuery("检查全部", view="regroup"))
            self.assertEqual(first.status, "pending")
            self.assertEqual(first.covered, (0,))
            service.retry_job(first.job_id, chunks=[1])
            second = await service.query(WikiQuery("检查全部", view="regroup"))
            self.assertEqual(second.status, "complete")
            self.assertEqual(second.covered, (0, 1, 2))
            self.assertEqual(len(calls), 6)
            job = json.loads((service.root / "jobs" / f"{first.job_id}.json").read_text())
            self.assertIn("merged", job)

    async def test_regroup_reply_cursor_does_not_advance_changed_job(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Client:
                async def chat(self, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        raise RuntimeError("interrupted chunk")
                    payload = json.loads(kwargs["messages"][1]["content"])
                    return SimpleNamespace(
                        content=json.dumps(
                            {"facts": [{"ref": payload["evidence"]["ref"], "text": "checked"}]}
                        )
                    )

            service = WikiService(
                root, Client, SimpleNamespace(protocol_attempts=1, temperature=0, max_tokens=1000)
            )
            service.register({"text": "原件" * 8000})
            first = await service.query(WikiQuery("范围", view="regroup"))
            self.assertTrue(first.cursor)
            self.assertEqual(first.status, "pending")
            service.retry_job(first.job_id, chunks=[0])
            latest = await service.query(WikiQuery("范围", view="regroup"))
            self.assertEqual(latest.status, "complete")
            count = len(calls)
            old_page = await service.query(WikiQuery("范围", view="regroup", cursor=first.cursor))
            self.assertEqual(old_page.status, "pending")
            self.assertEqual(len(calls), count)

    async def test_budget_grant_resumes_same_workspace_request_and_unknown_is_not_resent(self):
        from darwinagent.llm.client import BudgetExceeded

        with tempfile.TemporaryDirectory() as root:
            calls = []

            class Client:
                async def chat(self, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        raise BudgetExceeded("local fixture cap", dispatched=False)
                    ref = json.loads(kwargs["messages"][1]["content"])["evidence"]["ref"]
                    return SimpleNamespace(
                        content=json.dumps({"facts": [{"ref": ref, "text": "checked"}]})
                    )

            service = WikiService(
                root, Client, SimpleNamespace(protocol_attempts=1, temperature=0, max_tokens=1000)
            )
            service.register({"text": "original"})
            first = await service.query(WikiQuery("grant", view="regroup"))
            self.assertEqual(first.status, "pending")
            job = json.loads((service.root / "jobs" / f"{first.job_id}.json").read_text())
            self.assertEqual(job["blocked"]["state"], "awaiting_budget")
            step = next((service.root / "jobs").glob("*.steps/*.json"))
            request_id = json.loads(step.read_text())["request_id"]
            self.assertEqual(service.workspace.request(request_id)["status"], "prepared")
            second = await service.query(WikiQuery("grant", view="regroup"))
            self.assertEqual(second.status, "complete")
            self.assertEqual(service.workspace.request(request_id)["status"], "responded")
            payload = service.workspace.read_json(
                service.workspace.request(request_id)["payload_ref"]
            )
            self.assertEqual(payload["provenance"]["query_id"], first.job_id)
        with tempfile.TemporaryDirectory() as root:
            calls = []

            class UnknownClient:
                async def chat(self, **kwargs):
                    calls.append(kwargs)
                    raise RuntimeError("lost external response")

            service = WikiService(
                root,
                UnknownClient,
                SimpleNamespace(protocol_attempts=1, temperature=0, max_tokens=1000),
            )
            service.register({"text": "original"})
            first = await service.query(WikiQuery("unknown", view="regroup"))
            await service.query(WikiQuery("unknown", view="regroup"))
            self.assertEqual(len(calls), 1)
            step = next((service.root / "jobs").glob("*.steps/*.json"))
            request_id = json.loads(step.read_text())["request_id"]
            service.workspace.set_request_status(request_id, "abandoned")
            abandoned = await service.query(WikiQuery("unknown", view="regroup"))
            self.assertEqual(abandoned.status, "pending")
            job = json.loads((service.root / "jobs" / f"{first.job_id}.json").read_text())
            self.assertEqual(job["blocked"]["state"], "abandoned_request")
            self.assertEqual(len(calls), 1)

    async def test_raw_coverage_describes_returned_fragment_not_all_matched(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            service.register({"text": "一" * 16000})
            service.register({"text": "二" * 16000})
            reply = await service.query(WikiQuery("page", max_chars=8000))
            self.assertEqual(len(reply.matched), 2)
            self.assertEqual(len(reply.covered), 1)
            self.assertFalse(reply.covered[0]["fragment_complete"])
            self.assertGreater(len(reply.uncovered), 0)
            self.assertEqual(reply.status, "partial")

    async def test_serialized_reply_size_bounds_many_refs_and_acl(self):
        with tempfile.TemporaryDirectory() as root:
            service = WikiService(root)
            for i in range(100):
                service.register({"index": i})
            reply = await service.query(WikiQuery("全部引用", max_chars=2000))
            for _ in range(10):
                self.assertLessEqual(len(json.dumps(reply.to_dict(), ensure_ascii=False)), 2000)
                if not reply.cursor:
                    break
                reply = await service.query(
                    WikiQuery("全部引用", cursor=reply.cursor, max_chars=2000)
                )
            with self.assertRaises(PermissionError):
                service.register(
                    {"metrics": {"expected_answer": "secret"}}, source_kind="aggregate"
                )
            with self.assertRaises(PermissionError):
                service.register(
                    {"diagnostics": [{"answer": "test result"}]}, source_kind="aggregate"
                )
            with self.assertRaises(ValueError):
                await service.query(
                    WikiQuery("全部引用", cursor=json.dumps({"snapshot": "../judge", "offset": 0}))
                )

    def test_late_paging_hints_and_control_fields_survive_summary(self):
        trace = [
            {"stage": "tool", "parameters": {"offset": i}, "data": {"rows": [i]}} for i in range(8)
        ]
        trace.append(
            {
                "stage": "tool",
                "parameters": {"offset": 80},
                "data": {"rows": [], "next_offset": 80, "more_remain": True},
            }
        )
        selected = bounded_trace(trace, 4)
        self.assertIn(trace[-1], selected)
        facts = _context_facts({"training_examples": [{"trace": trace, "source_text": []}]})
        last = facts["training_examples"][0]["trace"][-1]
        self.assertEqual(last["pagination"]["data"]["next_offset"], 80)
        self.assertIn("offset_not_advanced", last["anomaly_hints"])
