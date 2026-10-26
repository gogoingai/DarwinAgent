import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.agent.context import AgentContext
from darwinagent.runtime.steps import StepJournal
from datasets.lift.report import turn_totals


class RetryAccountingTests(unittest.TestCase):
    def make_recovery(self, root):
        journal = StepJournal(root)
        payload = {"role": "worker", "messages": [{"role": "user", "content": "test"}]}
        path, _ = journal.reserve("model", payload)
        journal.submitted(path)
        original = path.read_bytes()
        retry = journal.retry_model(path, authorization="explicit test authorization")
        journal.respond(
            retry, {"content": "done", "usage": {"prompt_tokens": 17, "completion_tokens": 3}}
        )
        return journal, path, retry, original

    def test_completed_recovery_is_not_unknown_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as folder:
            journal, path, _, original = self.make_recovery(Path(folder))
            context = object.__new__(AgentContext)
            context.journal = journal
            usage = context.usage()
            self.assertEqual(usage["unknown_calls"], 0)
            self.assertEqual(usage["pending_calls"], 0)
            self.assertEqual(usage["prompt_tokens"], 17)
            self.assertEqual(usage["completion_tokens"], 3)
            self.assertEqual(path.read_bytes(), original)

    def test_report_uses_recovered_response(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal, _, _, _ = self.make_recovery(root / "episodes/turn-0/steps")
            totals = turn_totals(root, 0, {"episode": None})
            self.assertEqual(totals["usage"]["unknown_calls"], 0)

    def test_report_does_not_create_missing_archive_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            turn_totals(root, 0, {"episode": None})
            self.assertEqual(list(root.iterdir()), [])

    def test_report_can_read_archive_without_mkdir_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_recovery(root / "episodes/turn-0/steps")
            with patch.object(Path, "mkdir", side_effect=PermissionError("read-only archive")):
                self.assertEqual(
                    turn_totals(root, 0, {"episode": None})["usage"]["unknown_calls"], 0
                )

    def test_real_unknown_still_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = StepJournal(folder)
            path, _ = journal.reserve("model", {"role": "worker", "messages": []})
            journal.submitted(path)
            context = object.__new__(AgentContext)
            context.journal = journal
            self.assertEqual(context.usage()["unknown_calls"], 1)

    def test_tampered_original_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            journal, path, _, _ = self.make_recovery(Path(folder))
            row = json.loads(path.read_text())
            row["state"] = "prepared"
            path.write_text(json.dumps(row))
            context = object.__new__(AgentContext)
            context.journal = journal
            with self.assertRaises(ValueError):
                context.usage()


if __name__ == "__main__":
    unittest.main()
