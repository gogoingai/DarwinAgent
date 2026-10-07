"""Preflight extended public-chain fixtures with a substituted transport only."""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
module_spec = importlib.util.spec_from_file_location(
    "extended_smoke_fixture", ROOT / "scripts/smoke_extended.py"
)
extended = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(extended)


class ExtendedEntryTests(unittest.TestCase):
    def test_independent_collection_fields_reject_wrong_types_and_duplicates(self):
        def result(value):
            return SimpleNamespace(
                answers=[
                    SimpleNamespace(
                        question_id="q", status="answered", answer=json.dumps(value), node_ids=["n"]
                    )
                ]
            )

        expected = {"q": {"technicians": ["李", "王"], "count": 1}}
        self.assertEqual(
            extended.evaluate(result({"technicians": ["王", "李"], "count": 1}), expected)[
                "passed"
            ],
            1,
        )
        for value in (
            {"technicians": ["李", "李"], "count": 1},
            {"technicians": ["李", 2], "count": 1},
            {"technicians": ["李", "王"], "count": True},
        ):
            self.assertEqual(extended.evaluate(result(value), expected)["passed"], 0)

    def test_stress_corpus_matches_graph_and_never_contains_controller_references(self):
        import smoke_stress

        with tempfile.TemporaryDirectory() as root:
            definitions = smoke_stress.definitions()
            cases = extended.fixtures(Path(root), definitions)
            self.assertEqual(len(cases), 12)
            self.assertEqual(sum(len(c.questions) for c, *_ in cases), 28)
            for (case, snapshot, _, _, references), (_, records, questions) in zip(
                cases, definitions, strict=True
            ):
                graph = json.loads((snapshot / "graph.json").read_text())
                manifest = json.loads((snapshot / "manifest.json").read_text())
                self.assertEqual(manifest["n_facts"], len(records))
                self.assertEqual(len(graph["nodes"]), len(records))
                self.assertEqual(len(references), len(questions))
                self.assertNotIn("expected", json.dumps(case.to_dict()))
            case, snapshot, _, _, references = cases[0]
            folder = Path(root) / "generation" / case.id / "branches/main/answers"
            folder.mkdir(parents=True)
            case_path = snapshot / "case.json"
            case_path.write_text(json.dumps(case.to_dict()))
            answer = {
                "question_id": "q1",
                "status": "answered",
                "answer": json.dumps(references["q1"]),
                "node_ids": [],
                "trace": [],
            }
            record = {
                "result": answer,
                "digest": extended.digest(answer),
                "case_path": str(case_path),
                "case_digest": extended.digest(case.to_dict()),
                "graph_path": str(snapshot / "graph.json"),
                "graph_fingerprint": extended.digest(
                    json.loads((snapshot / "graph.json").read_text())
                ),
            }
            saved_path = folder / "fixture.json"
            saved_path.write_text(json.dumps(record))
            audit = smoke_stress.audit_saved_answers(Path(root))
            self.assertEqual(audit["completed_questions"], 1)
            self.assertEqual(audit["passed"], 1)
            self.assertEqual(len(audit["missing"]), 27)
            record["result"]["answer"] = "tampered"
            saved_path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "Answer checksum mismatch"):
                smoke_stress.audit_saved_answers(Path(root))

    def test_public_resume_fork_and_independent_fields_without_http(self):
        class FakeClient:
            def __init__(self, cfg):
                self.cfg = cfg
                self.calls = []

            async def chat(self, **request):
                self.calls.append(request)
                payload = json.loads(request["messages"][-1]["content"])
                role = request["role"]
                if role == "tools":
                    previous = payload["previous_results"]
                    question = payload["question"]
                    if "D-71" in question:
                        offset = 25 * len(previous)
                        response = (
                            {"action": "ready"}
                            if offset >= 75
                            else {
                                "action": "call",
                                "asset_id": "f_page_facts",
                                "parameters": {"serial": "D-71", "offset": offset, "limit": 25},
                            }
                        )
                    else:
                        response = (
                            {"action": "ready"}
                            if previous
                            else {
                                "action": "call",
                                "asset_id": "device_lookup",
                                "parameters": payload["parameters"],
                            }
                        )
                elif role == "answer":
                    serial = payload["parameters"]["serial"]
                    question = payload["question"]
                    expected = (
                        {"technician": "赵", "date": "2026-03-09"}
                        if serial == "D-32"
                        else {"technician": "林", "date": "2026-01-02"}
                        if serial == "D-31"
                        else {"technician": "李", "date": "2026-07-03"}
                        if serial == "D-51"
                        else {"count": 75}
                        if "多少" in question
                        else {"technician": "陈", "date": "2026-03-16"}
                    )
                    rows = payload["visible_evidence"]
                    response = {
                        "status": "abstained" if serial == "D-59" else "answered",
                        "answer": "信息不足"
                        if serial == "D-59"
                        else json.dumps(expected, ensure_ascii=False),
                        "node_ids": [] if serial == "D-59" else [row["node_id"] for row in rows],
                    }
                else:
                    response = {
                        "accepted": True,
                        "supported": True,
                        "subject_correct": True,
                        "consistent": True,
                        "complete": True,
                        "abstention_valid": True,
                        "feedback": "fixture-only",
                    }
                return SimpleNamespace(content=json.dumps(response, ensure_ascii=False))

            def http_attempts(self):
                return 0

            def ledger_summary(self):
                return {"fixture_calls": len(self.calls)}

            async def aclose(self):
                pass

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(extended.base, "LLMClient", FakeClient),
            patch.dict(
                "os.environ",
                {
                    "DARWINAGENT_BASE_URL": "http://fixture.invalid/v1",
                    "DARWINAGENT_API_KEY": "fixture-only",
                },
            ),
        ):
            report = asyncio.run(extended.live(Path(tmp), "explicit-fixture-model"))
            self.assertEqual(report["status"], "complete", report)
            self.assertEqual([c["evaluation"]["passed"] for c in report["cases"]], [2, 2, 2])
            self.assertEqual(report["http_attempts"], 0)
            self.assertTrue(report["receipt_resume"]["same_request_id"])
            self.assertTrue(all(c["reuse_new_http_attempts"] == 0 for c in report["cases"]))
            resumed = asyncio.run(extended.live(Path(tmp), "explicit-fixture-model"))
            self.assertEqual(resumed["status"], "complete", resumed)
            self.assertEqual(resumed["ledger"]["fixture_calls"], 0)
            original_run = extended.Pipeline.run

            async def blocked_case(pipeline, case, *args, **kwargs):
                if case.id == "historical-refusal":
                    raise extended.UnknownRequest("fixture unknown request; do not resend")
                return await original_run(pipeline, case, *args, **kwargs)

            with patch.object(extended.Pipeline, "run", blocked_case):
                partial = asyncio.run(extended.live(Path(tmp), "explicit-fixture-model"))
            self.assertEqual(partial["status"], "pending")
            self.assertEqual(
                [c["case_id"] for c in partial["cases"]], ["subject-date", "paging-tail"]
            )
            self.assertEqual(partial["unresolved"][0]["case_id"], "historical-refusal")
            self.assertEqual(partial["ledger"]["fixture_calls"], 0)
