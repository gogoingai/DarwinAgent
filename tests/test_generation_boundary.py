import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import networkx as nx

from oak.config import Config
from oak.contracts import QuestionInput
from oak.engine import InferenceEngine
from datasets.locomo.pipeline.agent import QAOutput
from datasets.locomo.pipeline.config import LocomoConfig
from datasets.locomo.pipeline.runner import answer_all


class GenerationBoundary(unittest.TestCase):
    def fixtures(self, root):
        class EvaluationQA:
            idx = 0
            question = "Which device?"
            @property
            def answer(self):
                raise AssertionError("Reference reached generation")
            @property
            def category(self):
                raise AssertionError("Evaluation category reached generation")
        conv = SimpleNamespace(sample_id="conv-26", qas=[EvaluationQA()], header=lambda: "source header")
        toolbox = SimpleNamespace(g=nx.MultiDiGraph())
        cfg = Config(api_key="fake", work_dir=root)
        return conv, toolbox, SimpleNamespace(cfg=cfg), LocomoConfig(cfg, root / "unused.json")

    def test_engine_receives_reference_free_questions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            values = self.fixtures(root)
            original = InferenceEngine.run
            async def inspect(engine, questions, **kwargs):
                self.assertTrue(all(isinstance(q, QuestionInput) and not hasattr(q, "answer") for q in questions))
                return await original(engine, questions, **kwargs)
            async def fake_answer(idx, question, *args):
                return QAOutput(idx, question, answer="A", evidence=["f1"])
            with patch.object(InferenceEngine, "run", inspect), patch("datasets.locomo.pipeline.runner.run_qa", fake_answer):
                out = asyncio.run(answer_all(*values, root / "answers"))
            self.assertEqual(out[0].answer, "A")
            # A JSON roundtrip must preserve identity without rerunning the model.
            with patch("datasets.locomo.pipeline.runner.run_qa", side_effect=AssertionError("Unexpected rerun")):
                resumed = asyncio.run(answer_all(*values, root / "answers"))
            self.assertEqual(resumed[0].answer, "A")

    def test_resume_cannot_silently_drop_checkpointed_question(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            values = self.fixtures(root)
            async def fake_answer(idx, question, *args):
                return QAOutput(idx, question, answer="A", evidence=["f1"])
            with patch("datasets.locomo.pipeline.runner.run_qa", fake_answer):
                asyncio.run(answer_all(*values, root / "answers"))
            (root / "answers/answers.jsonl").write_text("")
            with self.assertRaises(ValueError):
                asyncio.run(answer_all(*values, root / "answers"))
