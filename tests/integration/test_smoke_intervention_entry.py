"""Live entry exercised with a substituted transport: no real HTTP or models."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = importlib.util.spec_from_file_location(
    "intervention_smoke_fixture", ROOT / "scripts/smoke_intervention.py"
)
smoke = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(smoke)


class SmokeEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_requires_explicit_model_before_constructing_transport(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(smoke, "LLMClient") as transport:
            with self.assertRaises(ValueError):
                await smoke.live(Path(tmp), "")
            transport.assert_not_called()

    async def test_live_public_chain_only_uses_explicit_connection_and_model(self):
        instances = []

        class FakeTransport(smoke.DialogueClient):
            def __init__(self, cfg):
                case, *_ = smoke.prepare_fixture(cfg.work_dir.parent, "live")
                base = smoke.recorded_generation_client(case)
                replies = {key: list(value) for key, value in base.replies.items()}
                replies["proposal"] = [
                    {
                        "action": "query_wiki",
                        "query": {
                            "question": "检查分页原件",
                            "scope": {"case_ids": [case.id], "question_ids": ["q3"]},
                            "view": "regroup",
                        },
                    },
                    {"action": "no_change", "reason": "fake-only entry verification"},
                ]
                super().__init__(replies)
                self.cfg = cfg
                instances.append(self)

            def http_attempts(self):
                return 0

            def ledger_summary(self):
                return {"fixture_transport_calls": len(self.calls)}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(smoke, "LLMClient", FakeTransport),
            patch.dict(
                "os.environ",
                {
                    "DARWINAGENT_BASE_URL": "http://offline.invalid/v1",
                    "DARWINAGENT_API_KEY": "fixture-only",
                    "DARWINAGENT_MODEL": "unselected-environment-model",
                },
            ),
        ):
            report = await smoke.live(Path(tmp), "explicit-fixture-model")
            self.assertEqual(report["status"], "complete", report)
            self.assertEqual(report["evaluation"]["metrics"]["fixture_exact"], 5)
            self.assertEqual(report["dynamic_exchanges"], 1)
            self.assertEqual(report["reuse_new_http_attempts"], 0)
            cfg = instances[0].cfg
            self.assertEqual(
                {cfg.model_strong, cfg.model_middle, cfg.model_fast}, {"explicit-fixture-model"}
            )
            self.assertEqual(cfg.max_http_requests, 80)
            self.assertEqual(cfg.max_concurrency, 1)
            self.assertIsNotNone(cfg.deadline_monotonic)
            with self.assertRaises(ValueError):
                smoke.prepare_fixture(Path(tmp), "replay")
