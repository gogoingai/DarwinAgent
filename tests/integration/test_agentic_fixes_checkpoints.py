"""Offline regression scenarios for checkpoints."""

import json
import tempfile
import unittest
from pathlib import Path


class BaselineCompatibilityTests(unittest.TestCase):
    """评审五：共同条件核对实际生效——模型/判题器/作答配置/快照任一不同即不兼容。"""

    def _ident(self, **over):
        base = {
            "transport": {"model_strong": "glm-5.3"},
            "judge_lock": "j1",
            "run_config": {
                "protocol_attempts": 5,
                "answer_attempts": 3,
                "temperature": 0.2,
                "max_tokens": 4096,
                "calls_per_question": 32,
                "retrieval_mode": "agentic",
                "vector_k": 30,
            },
            "snapshots": {"conv-30": "d1"},
            "asset_version": "v1",
        }
        base.update(over)
        return base

    def test_expected_arm_differences_stay_compatible(self):
        from datasets.locomo.scripts.external_test import baseline_compatibility

        other_arm = self._ident(
            run_config={
                "protocol_attempts": 5,
                "answer_attempts": 3,
                "temperature": 0.2,
                "max_tokens": 4096,
                "calls_per_question": 32,
                "retrieval_mode": "vector_once",
                "vector_k": 60,
            },
            asset_version="v2",
        )
        self.assertEqual(baseline_compatibility(self._ident(), other_arm), "compatible")

    def test_real_differences_rejected(self):
        from datasets.locomo.scripts.external_test import baseline_compatibility

        mine = self._ident()
        cases = {
            "模型路由不同": (self._ident(transport={"model_strong": "glm-4.7"}), "模型路由不同"),
            "判题器锁不同": (self._ident(judge_lock="j2"), "判题器锁不同"),
            "作答/审查配置不同": (
                self._ident(
                    run_config={
                        "protocol_attempts": 3,
                        "answer_attempts": 3,
                        "temperature": 0.2,
                        "max_tokens": 4096,
                        "calls_per_question": 32,
                        "retrieval_mode": "vector_once",
                        "vector_k": 60,
                    }
                ),
                "作答/审查配置不同",
            ),
            "快照身份不同": (self._ident(snapshots={"conv-30": "d2"}), "快照身份不同"),
            "身份缺失": (None, "缺少身份记录"),
        }
        for label, (base, marker) in cases.items():
            verdict = baseline_compatibility(mine, base)
            self.assertTrue(verdict.startswith("incompatible"), label)
            self.assertIn(marker, verdict, label)


class CarriedCheckpointTests(unittest.TestCase):
    """答案检查点跨框架版本搬运（用户指令：不要从头跑）：CARRIED 旁车＋答案路径逐字节复核。
    编排/评测层（darwinagent/experiments/）差异不影响答案计算，可重锚；答案路径漂移一律拒绝。"""

    def test_no_sidecar_accepts_only_current_identity(self):
        from darwinagent.engine.pipeline import carried_acceptor

        with tempfile.TemporaryDirectory() as td:
            acc = carried_acceptor(td, "new-id", {})
            self.assertTrue(acc("new-id"))
            self.assertFalse(acc("old-id"))

    def test_sidecar_accepts_old_identity_when_answer_path_identical(self):
        from darwinagent.engine.pipeline import carried_acceptor

        fw = {
            "/x/darwinagent/operators/data.py": "a",
            "/x/darwinagent/experiments/runner.py": "old",
        }
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "CARRIED.json").write_text(
                json.dumps({"accepted_identities": ["old-id"], "source_framework": fw})
            )
            self.assertTrue(carried_acceptor(td, "new-id", fw)("old-id"))
            fw2 = dict(fw)
            fw2["/x/darwinagent/experiments/runner.py"] = "new"
            self.assertTrue(carried_acceptor(td, "new-id", fw2)("old-id"))  # 编排层差异放行

    def test_sidecar_rejects_when_answer_path_changed(self):
        from darwinagent.engine.pipeline import carried_acceptor

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "CARRIED.json").write_text(
                json.dumps(
                    {
                        "accepted_identities": ["old-id"],
                        "source_framework": {"/x/darwinagent/operators/data.py": "a"},
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "答案路径文件与搬运源不一致"):
                carried_acceptor(td, "new-id", {"/x/darwinagent/operators/data.py": "changed"})

    def test_carry_rebase_answer_path_drift_detected(self):
        from datasets.locomo.scripts.carry_rebase import answer_path_ok

        bad = answer_path_ok(
            {
                "/x/darwinagent/operators/data.py": "a",
                "/x/darwinagent/experiments/runner.py": "old",
            },
            {
                "/x/darwinagent/operators/data.py": "b",
                "/x/darwinagent/experiments/runner.py": "new",
            },
        )
        self.assertEqual(bad, ["/x/darwinagent/operators/data.py"])
