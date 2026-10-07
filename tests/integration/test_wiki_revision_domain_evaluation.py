"""Offline regression scenarios for domain evaluation."""

import asyncio
import unittest

from darwinagent.contracts import QuestionInput


class EvaluatorInterfaceRegression(unittest.TestCase):
    def test_generic_non_numeric_question_and_opt_in_subset(self):
        from darwinagent.experiments.runner import _evaluate_stage

        result = object()
        questions = (QuestionInput("trip-A", "where?"),)
        seen = []

        class GenericEvaluator:
            async def evaluate(self, value):
                seen.append(value)
                return "generic"

        class SubsetEvaluator:
            async def evaluate(self, value, *, asked):
                seen.append((value, asked))
                return "subset"

        self.assertEqual(
            asyncio.run(_evaluate_stage(GenericEvaluator(), result, questions)), "generic"
        )
        self.assertEqual(
            asyncio.run(_evaluate_stage(SubsetEvaluator(), result, questions)), "subset"
        )
        self.assertEqual(seen, [result, (result, ("trip-A",))])
