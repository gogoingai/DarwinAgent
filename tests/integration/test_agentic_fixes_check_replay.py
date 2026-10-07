"""Offline regression scenarios for check replay."""

import unittest


class AnswerCheckAdmissionTests(unittest.TestCase):
    """agentic_v6 G1 B0 全灭事故回归：答案阶段 C 结构不兼容/全盘否决必须在准入被拒。"""

    ROWS = [
        {
            "node_id": "n000000",
            "entity_type": "原子事实",
            "陈述": "甲计划下周修打印机",
            "source_ids": ["s"],
            "编号": "c-0001",
            "主体": "甲",
        }
    ]

    def _registry(self, src, check_stage="answer"):
        from darwinagent.kernel.checks import CheckRegistry
        from darwinagent.operators.sandbox import Limits

        target_stage = check_stage

        class A:
            kind = "C"
            id = "c"
            fingerprint = "f"
            stage = target_stage
            content = src

        class B:
            assets = type("AS", (), {"assets": (A(),)})()
            version = "v"

            def verify(self):
                pass

        return CheckRegistry(B(), Limits(30000, 15.0, 180000))

    def test_synthetic_snapshot_is_well_formed(self):
        from darwinagent.kernel.checks import synthetic_answer_snapshot

        snap = synthetic_answer_snapshot(self.ROWS, "甲计划做什么？")
        self.assertEqual(snap["status"], "answered")
        self.assertEqual(snap["answer"], "甲计划下周修打印机")
        self.assertTrue(snap["evidence"] and snap["node_ids"])

    def test_rejecting_c_fails_admission(self):
        from darwinagent.kernel.checks import enforce_opinions, synthetic_answer_snapshot

        snap = synthetic_answer_snapshot(self.ROWS, "甲计划做什么？")
        bad = self._registry(
            "def check(candidate):\n return {'ok': False, 'issues': ['candidate 不是对象']}"
        )
        with self.assertRaises(ValueError) as caught:
            enforce_opinions(bad.run("answer", snap), "冷启动答案阶段")
        self.assertIn("candidate 不是对象", str(caught.exception))
        sane = self._registry("def check(candidate):\n return {'ok': True, 'issues': []}")
        enforce_opinions(sane.run("answer", snap), "冷启动答案阶段")  # 正常 C 通过


class DecorativeCheckTests(unittest.TestCase):
    """非法候选用例（专家缺口）：存在答案阶段 C 时，畸形候选必须被至少一个 C 拒绝；
    装饰性 C（永远 ok）在准入被拒。"""

    def test_all_ok_check_rejected_and_flagging_check_passes(self):
        from darwinagent.kernel.checks import enforce_rejection

        enforce_rejection([], "ctx")  # 无 C＝合法省略
        with self.assertRaisesRegex(ValueError, "畸形候选"):
            enforce_rejection([{"check_id": "c1", "ok": True, "issues": []}], "ctx")
        enforce_rejection([{"check_id": "c1", "ok": False, "issues": ["空答案"]}], "ctx")

    def test_invalid_variant_shape(self):
        from darwinagent.kernel.checks import synthetic_invalid_answer_snapshot

        v = synthetic_invalid_answer_snapshot("问？")
        self.assertEqual(v["status"], "answered")
        self.assertEqual(v["answer"], "")
        self.assertEqual(v["node_ids"], [])
