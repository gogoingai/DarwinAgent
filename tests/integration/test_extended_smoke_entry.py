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
