"""Offline regression scenarios for travel checks."""

import json
import unittest

from darwinagent.kernel import TaskSpec
from tests.support.travel_faults import TASK_YAML, travel_case, travel_graph


class ReviewFixUnitTests(unittest.TestCase):
    """2026-10-05 审查修复的最小单元：占位实例变序、大数值压力样本、
    维护证据压缩、verified_fix 场景绑定。"""

    def test_multi_instance_days_are_varied(self):
        from darwinagent.kernel.checks import synthetic_answer_battery

        spec = TaskSpec.load(TASK_YAML)
        battery = synthetic_answer_battery(
            [{"node_id": "n1", "x": 1}], "q", {}, spec.answer_contract
        )
        multi = [
            s
            for _, s in battery
            if isinstance(s.get("structured_answer"), list) and len(s["structured_answer"]) > 1
        ]
        self.assertTrue(multi)
        days = [d["days"] for d in multi[0]["structured_answer"]]
        self.assertEqual(days, [1, 2])  # 内部一致的天序，不再复制出 days=[1,1]

    def test_large_numeric_stress_variant(self):
        from darwinagent.experiments.trials import stress_trial_samples

        graph = travel_graph(travel_case())
        out = stress_trial_samples(
            [{"subject": "x", "fact_type": "", "date_prefix": "", "limit": 20}], graph
        )
        self.assertTrue(any(isinstance(p, dict) and p.get("limit", 0) >= 500 for p in out), out)

    def test_wiki_evidence_compression_keeps_facts(self):
        from darwinagent.experiments.wiki import _compress_training_evidence

        facts = {
            "training_examples": [
                {
                    "question_id": "0",
                    "parameters": {"a": 1},
                    "answer": "x" * 50000,
                    "baseline_answer": "y" * 5000,
                    "rows": [{"r": i} for i in range(50)],
                    "node_ids": [f"n{i}" for i in range(40)],
                    "source_text": [{"text": "z" * 3000, "id": f"s{i}"} for i in range(8)],
                    "candidate_json_type": "list",
                    "checks": [
                        {"check_id": "c_answer_shape", "issues": ["answer is not an array"]}
                    ],
                    "steps_used": 45,
                }
            ]
        }
        _compress_training_evidence(facts)
        example = facts["training_examples"][0]
        self.assertLess(len(json.dumps(facts, ensure_ascii=False)), 6000)
        self.assertEqual(example["question_id"], "0")
        self.assertEqual(
            example["checks"],
            [{"check_id": "c_answer_shape", "issues": ["answer is not an array"]}],
        )
        self.assertEqual(example["candidate_json_type"], "list")
        self.assertEqual(example["steps_used"], 45)
        self.assertLess(len(example["answer"]), 700)
        self.assertEqual(example["rows"][:1][0], {"r": 0})
        self.assertEqual(example["rows_total"], 50)

    def test_verified_fix_requires_scenario_reproduction(self):
        """审查 P2 反例：旧 C 失败＋同资产过门＋空 scenarios 不得标记已验证修复。"""
        from darwinagent.experiments.wiki import _lessons

        entries = [
            {
                "id": "a" * 64,
                "stage": "R1",
                "kind": "attempt",
                "category": "runtime",
                "scope": "admission",
                "training_ids": [],
                "fact_status": "recorded",
                "confidence": "hypothesis",
                "pending_attribution": False,
                "facts": {
                    "status": "failed",
                    "admission": {
                        "scenarios": [
                            {
                                "asset_id": "c_answer_shape",
                                "scenario_id": "answer_0",
                                "status": "failed",
                                "required": True,
                                "error_type": "CandidateCheckRejected",
                                "error": "valid array rejected",
                            }
                        ]
                    },
                },
            },
            {
                "id": "b" * 64,
                "stage": "R2",
                "kind": "attempt",
                "category": "strategy",
                "scope": "admission",
                "training_ids": [],
                "fact_status": "recorded",
                "confidence": "hypothesis",
                "pending_attribution": False,
                "facts": {
                    "status": "passed",
                    "asset_changes": [
                        {
                            "asset_id": "c_answer_shape",
                            "after": {
                                "id": "c_answer_shape",
                                "kind": "C",
                                "content": 'def check(c):\n    return {"ok": True, "issues": []}\n',
                                "input_contract": {"type": "any"},
                                "output_contract": {"type": "any"},
                                "trial_inputs": [],
                                "fingerprint": "f" * 64,
                            },
                        }
                    ],
                    "verification": {"verdict": "passed", "scenarios": []},
                },
            },
        ]
        lessons = _lessons(entries)
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )
