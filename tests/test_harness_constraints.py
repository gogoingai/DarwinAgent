import asyncio
import json
import unittest
from types import SimpleNamespace

from datasets.locomo.pipeline.agent import _consensus_pick
from datasets.locomo.pipeline.prompts.answer import REFUSAL


class HarnessConstraints(unittest.TestCase):
    def test_missing_event_constraint_blocks_otherwise_positive_review(self):
        row = {"index": 0, "supported": True, "subject_correct": True,
               "consistent": True, "complete": True, "reason": "Person matches but event does not",
               "requirements": [{"text": "person", "supported": True, "evidence": ["1"]},
                                {"text": "after requested event", "supported": False, "evidence": []}]}
        class Client:
            async def chat(self, **kwargs):
                return SimpleNamespace(content=json.dumps({"pick": -1, "reviews": [row]}))
        result = asyncio.run(_consensus_pick("How did A feel after the race?", [("Free", ["1"])],
            Client(), "train", {"1": "A felt free after coming out"}, requirements_review=True))
        self.assertEqual(result, (REFUSAL, []))

    def test_requirement_support_cannot_invent_reference(self):
        row = {"index": 0, "supported": True, "subject_correct": True,
               "consistent": True, "complete": True, "reason": "invented",
               "requirements": [{"text": "race", "supported": True, "evidence": ["missing"]}]}
        class Client:
            async def chat(self, **kwargs):
                return SimpleNamespace(content=json.dumps({"pick": 0, "reviews": [row]}))
        from datasets.locomo.pipeline.agent import AnswerExecutionError
        with self.assertRaises(AnswerExecutionError):
            asyncio.run(_consensus_pick("q", [("A", ["1"])], Client(), "train", {"1": "fact"}, requirements_review=True))
