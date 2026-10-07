"""Offline regression scenarios for admission."""

import unittest

from darwinagent.contracts import EvaluationResult


class ExternalFaultsDoNotBlockAdoption(unittest.TestCase):
    """B：split_faults 分类＋采纳门只拦确定性族。"""

    @staticmethod
    def _scores(precise, faults=(), total=10):
        diag = tuple(
            {"question_id": str(i), "status": "execution_error", "error": e}
            for i, e in enumerate(faults)
        )
        completed = total - len(faults)
        return EvaluationResult(
            {"precise": precise, "lenient": precise}, total, completed, len(faults), 0, diag
        )

    def test_split_faults_classifies_by_error_prefix(self):
        from darwinagent.experiments.policy import split_faults

        s = self._scores(
            5,
            faults=(
                "TransportExhausted: tools: 429",
                "RateLimitError: x",
                "ValueError: tool.params: undeclared",
            ),
        )
        self.assertEqual(split_faults(s), (2, 1))

    def test_external_fault_does_not_block_deterministic_does(self):
        from darwinagent.experiments.policy import AdoptionPolicy

        policy = AdoptionPolicy("precise", ("lenient",))
        baseline = self._scores(5)
        # 外部故障候选：primary 严格升 → 采纳，且披露 external_faults
        ext = self._scores(6, faults=("TransportExhausted: tools: 429",))
        d = policy.decide(baseline, ext)
        self.assertTrue(d["accepted"], d["reasons"])
        self.assertEqual(d["external_faults"], {"baseline": 0, "candidate": 1})
        # 确定性故障候选：同分数 → 拒（incomplete_evaluation）
        det = self._scores(6, faults=("ValueError: tool.params: undeclared",))
        d2 = policy.decide(baseline, det)
        self.assertFalse(d2["accepted"])
        self.assertIn("incomplete_evaluation", d2["reasons"])
        # 外部故障不抬分：primary 不升仍拒
        ext_flat = self._scores(5, faults=("TransportExhausted: tools: 429",))
        d3 = policy.decide(baseline, ext_flat)
        self.assertFalse(d3["accepted"])
