"""Reused answers keep their original corpus and producer when inputs change."""

import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, CorpusBlock, RunResult
from darwinagent.engine.pipeline import Pipeline
from darwinagent.experiments.feedback import _wiki_training_evidence
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.execution import ExecutionSelection
from tests.support.device import case, client, spec


class WikiAnswerProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_corpus_continue_keeps_original_answer_and_wiki_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = case()
            task = spec(root / "assets")
            first = await Pipeline(client(original), root / "generation").run(
                original, task, RunConfig(concurrency=1), execution=ExecutionSelection()
            )
            changed = CaseInput(
                original.id,
                tuple(
                    CorpusBlock(b.source, b.text.replace("林", "张"), b.metadata)
                    for b in original.corpus
                ),
                original.questions,
            )
            unused = RecordedClient({})
            resumed = await Pipeline(unused, root / "generation").run(
                changed, task, RunConfig(concurrency=2), execution=ExecutionSelection()
            )
            self.assertEqual(unused.calls, [])
            self.assertEqual(resumed.answers, first.answers)
            self.assertNotEqual(resumed.identity, first.identity)
            evidence = _wiki_training_evidence((changed,), (resumed,))
            saved = evidence["_original_training_evidence"][0]
            self.assertEqual(saved["run_identity"], first.identity)
            self.assertEqual(saved["asset_version"], first.asset_version)
            self.assertEqual(saved["graph_fingerprint"], first.graph_fingerprint)
            self.assertIn("林", saved["source_text"][0]["text"])
            self.assertNotIn("张", saved["source_text"][0]["text"])
            self.assertIn("林", evidence["training_examples"][0]["source_text"][0]["text"])

    def test_missing_old_source_is_unknown_not_current_corpus(self):
        original = case(technician="张")
        from darwinagent.contracts import AnswerResult

        answer = AnswerResult("q1", "abstained", "信息不足")
        legacy = RunResult(original.id, "new-selected-view", "new-asset", (answer,), 0)
        saved = _wiki_training_evidence((original,), (legacy,))["_original_training_evidence"][0]
        self.assertIsNone(saved["run_identity"])
        self.assertEqual(saved["source_status"], "unknown")
        self.assertEqual(saved["source_text"], [])
