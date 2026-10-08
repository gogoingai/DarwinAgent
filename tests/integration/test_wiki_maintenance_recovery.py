"""Maintenance request files must preserve recovery, not permanently block attribution."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.config import RunConfig
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.artifacts import atomic_json

VALID = {"cause": "待验证原因", "action": "检查原件", "training_ids": []}


class Client(RecordedClient):
    async def aclose(self):
        pass


class WikiMaintenanceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def wiki(self, root, client):
        return WikiMaintainer(
            root, "fixture-source", lambda _: client, RunConfig(protocol_attempts=1), limit=10
        )

    async def test_unknown_request_file_blocks_until_explicit_retry_and_done_outbox_is_independent(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client({"wiki_maintainer": [RuntimeError("lost reply"), VALID]})
            wiki = self.wiki(tmp, client)
            event_id = await wiki.record("R1", "decision", {"status": "recorded"}, infer=True)
            request = wiki.root / "maintenance" / f"{event_id}.request.json"
            failure = wiki.root / "maintenance" / f"{event_id}.failure.json"
            request_bytes, failure_bytes = request.read_bytes(), failure.read_bytes()
            self.assertEqual(len(client.calls), 1)
            blocked = await wiki.resume_maintenance(event_id)
            self.assertEqual(blocked[0]["state"], "unknown")
            self.assertEqual(len(client.calls), 1)
            outbox = Path(tmp) / "R1/wiki-outbox/task.json"
            atomic_json(outbox, {"state": "done"})
            control = wiki.retry_maintenance(event_id, "operator confirmed a new attempt")
            self.assertTrue(control["possible_duplicate_cost"])
            result = await wiki.resume_maintenance(event_id)
            self.assertEqual(result[0]["state"], "complete")
            self.assertEqual(len(client.calls), 2)
            self.assertFalse(client.calls[-1]["use_cache"])
            self.assertEqual(request.read_bytes(), request_bytes)
            self.assertEqual(failure.read_bytes(), failure_bytes)
            self.assertEqual(json.loads(outbox.read_text())["state"], "done")
            self.assertFalse(wiki._wiki()["entries"][0]["pending_attribution"])
            self.assertEqual(json.loads(wiki.state_path.read_text())["reserved_calls"], 2)

    async def test_receipt_only_resume_publishes_attribution_without_repeat_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client({"wiki_maintainer": [VALID]})
            wiki = self.wiki(tmp, client)
            real_atomic = atomic_json

            def interrupted_publish(path, value):
                path = Path(path)
                if (
                    path.parent == wiki.root / "maintenance"
                    and isinstance(value, dict)
                    and "attribution" in value
                ):
                    raise OSError("interrupted after reliable response")
                return real_atomic(path, value)

            with patch("darwinagent.experiments.wiki.atomic_json", side_effect=interrupted_publish):
                event_id = await wiki.record("R1", "decision", {}, infer=True)
            self.assertEqual(len(client.calls), 1)
            steps = list((wiki.root / "maintenance" / event_id).glob("*.steps/*.json"))
            self.assertEqual(json.loads(steps[0].read_text())["state"], "responded")
            result = await wiki.resume_maintenance(event_id)
            self.assertEqual(result[0]["state"], "complete")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(json.loads(wiki.state_path.read_text())["reserved_calls"], 1)

    async def test_legacy_request_without_journal_imports_unknown_and_requires_explicit_attempt(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client({"wiki_maintainer": [VALID]})
            wiki = self.wiki(tmp, client)
            event_id = "a" * 64
            event = {
                "id": event_id,
                "stage": "old",
                "kind": "decision",
                "category": "strategy",
                "scope": "formal",
                "training_ids": [],
                "facts": {},
                "source": "legacy",
                "infer": True,
            }
            atomic_json(wiki.root / "events" / f"{event_id}.json", event)
            request = wiki.root / "maintenance" / f"{event_id}.request.json"
            payload = wiki._maintenance_payload(event)
            atomic_json(request, payload)
            original = request.read_bytes()
            blocked = await wiki.resume_maintenance(event_id)
            self.assertEqual(blocked[0]["state"], "unknown")
            self.assertEqual(client.calls, [])
            control = json.loads(
                (wiki.root / "maintenance" / f"{event_id}.control.json").read_text()
            )
            self.assertEqual(
                wiki.service.workspace.request(control["legacy_request_id"])["status"], "unknown"
            )
            wiki.retry_maintenance(event_id, "legacy outcome cannot be recovered")
            result = await wiki.resume_maintenance(event_id)
            self.assertEqual(result[0]["state"], "complete")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(request.read_bytes(), original)

    async def test_targeted_resume_does_not_execute_noninfer_or_other_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client({"wiki_maintainer": [VALID]})
            wiki = self.wiki(tmp, client)
            event_id = await wiki.record("R1", "formal", {}, infer=False)
            with self.assertRaises(ValueError):
                await wiki.resume_maintenance(event_id)
            with self.assertRaises(ValueError):
                wiki.retry_maintenance(event_id, "retry")
            self.assertEqual(await wiki.resume_maintenance(), [])
            self.assertEqual(client.calls, [])
