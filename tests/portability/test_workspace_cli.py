"""Offline public workspace commands, without model discovery or invocation."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from darwinagent.cli import main
from darwinagent.experiments.wiki_service import WikiService
from darwinagent.runtime.workspace import Workspace


class WorkspaceCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch(
                "darwinagent.llm.client.LLMClient", side_effect=AssertionError("No model client")
            ),
        ):
            status = main(["workspace", "--root", str(self.root / "run"), *map(str, args)])
        self.assertEqual(0, status, err.getvalue())
        return json.loads(out.getvalue())

    def test_human_selection_fork_and_portable_transfer(self):
        change = self.root / "change.json"
        change.write_text('{"kind":"select","candidate":"manual"}')
        result = self.invoke("intervene", change)
        self.assertEqual("manual", result["branch"]["working"])
        result = self.invoke("fork", "test")
        self.assertEqual("manual", result["working"])
        result = self.invoke("status")
        self.assertEqual(2, len(result["branches"]))
        self.invoke("export", self.root / "bundle")
        self.invoke("import", self.root / "bundle")
        self.assertEqual("manual", Workspace(self.root / "run/workspace").branch("main")["working"])

    def test_unknown_request_can_be_recovered_or_retried_without_dispatch(self):
        workspace = Workspace(self.root / "run/workspace")
        request = workspace.prepare_request({"messages": ["hi"]})
        workspace.submit_request(request)
        self.invoke("request", "recover", "--executor-stopped")
        self.assertEqual("unknown", workspace.request(request)["status"])
        retry = self.invoke("request", "resolve", request, "--action", "new-attempt")
        self.assertNotEqual(request, retry["request_id"])
        self.assertEqual("prepared", workspace.request(retry["request_id"])["status"])
        self.invoke("request", "resolve", request, "--action", "abandon")
        self.assertEqual("abandoned", workspace.request(request)["status"])

    def test_raw_wiki_and_regroup_without_model_return_durable_evidence(self):
        service = WikiService(self.root / "run")
        service.register({"offset": 5, "next_offset": 5, "more_remain": True})
        reply = self.invoke("wiki-query", "check progress")
        self.assertTrue(reply["evidence_refs"])
        self.assertEqual("complete", reply["status"])
        regroup = self.invoke("wiki-query", "check progress", "--view", "regroup")
        self.assertEqual("pending", regroup["status"])
        self.assertTrue(regroup["job_id"])

    def test_preview_is_scoped_and_does_not_construct_a_model(self):
        cases = self.root / "cases.json"
        cases.write_text(
            json.dumps(
                [
                    {
                        "id": "sample",
                        "questions": [{"id": "q1", "text": "one"}, {"id": "q2", "text": "two"}],
                    }
                ]
            )
        )
        selection = self.root / "selection.json"
        selection.write_text('{"question_ids":["q2"],"stages":["score"]}')
        result = self.invoke("preview", cases, "--selection", selection)
        self.assertEqual([], result["execute"])
        self.assertEqual("q2", result["missing"][0]["question_id"])
