import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.contracts import AnswerResult, QuestionInput
from darwinagent.experiments.control import preview
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.continuation import (
    fork_case_progress,
    register_legacy_case,
    reuse_preparation,
)
from darwinagent.runtime.execution import ExecutionSelection


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = SimpleNamespace(
            id="sample", questions=tuple(QuestionInput(str(i), f"question {i}") for i in range(120))
        )
        self.case_root = self.root / "generation" / self.case.id
        self.case_root.mkdir(parents=True)
        graph = {"nodes": [], "edges": []}
        atomic_json(self.case_root / "graph.json", graph)
        atomic_json(self.case_root / "graph.complete.json", {"digest": digest(graph)})
        for q in self.case.questions[:80]:
            answer = AnswerResult(q.id, "abstained", "insufficient information").to_dict()
            atomic_json(
                self.case_root / "answers" / (digest(q.id) + ".json"),
                {"identity": "legacy-source", "result": answer, "digest": digest(answer)},
            )

    def test_80_successes_40_pending_fork_reuses_only_bound_versions(self):
        report = register_legacy_case(self.case_root, self.case, source_case=self.case)
        self.assertEqual(80, len(report["registered"]))
        forked = fork_case_progress(self.case_root, "main", "alternative", self.case.questions)
        self.assertEqual(80, len(forked["registered"]))
        plan = preview(
            self.root, [self.case], ExecutionSelection(branch="alternative", stages=("answer",))
        )
        self.assertEqual(80, len(plan.reuse))
        self.assertEqual(40, len(plan.execute))
        fake_calls = []
        for item in plan.execute:
            fake_calls.append(item["question_id"])
        self.assertEqual([str(i) for i in range(80, 120)], fake_calls)
        self.assertEqual(80, len(list((self.case_root / "answers").glob("*.json"))))

    def test_same_id_new_question_does_not_use_old_answer(self):
        changed = SimpleNamespace(id="sample", questions=(QuestionInput("0", "new body"),))
        report = register_legacy_case(self.case_root, changed, source_case=self.case)
        self.assertEqual([], report["registered"])
        self.assertIn("version differs", report["not_registered"][0]["reason"])

    def test_unknown_question_binding_cannot_be_guessed(self):
        report = register_legacy_case(self.case_root, self.case)
        self.assertEqual([], report["registered"])
        self.assertIn("unavailable", report["not_registered"][0]["reason"])

    def test_graph_is_retained_independently_of_source_path(self):
        report = register_legacy_case(self.case_root, self.case, source_case=self.case)
        row = json.loads(Path(report["registered"][0]).read_text())
        (self.case_root / "graph.json").write_text("changed")
        self.assertNotEqual(self.case_root / "graph.json", Path(row["graph_path"]))
        self.assertEqual(
            row["graph_fingerprint"], digest(json.loads(Path(row["graph_path"]).read_text()))
        )

    def test_schema_change_can_copy_facts_without_copying_graph(self):
        facts = {"facts": ["saved fact"]}
        atomic_json(self.case_root / "memory.json", facts)
        atomic_json(self.case_root / "memory.complete.json", {"digest": digest(facts)})
        destination = self.root / "new-schema"
        result = reuse_preparation(self.case_root, destination)
        self.assertIn("memory.json", result["reused"])
        self.assertFalse((destination / "graph.json").exists())
        self.assertEqual(
            (self.case_root / "memory.json").read_bytes(),
            (destination / "memory.json").read_bytes(),
        )
