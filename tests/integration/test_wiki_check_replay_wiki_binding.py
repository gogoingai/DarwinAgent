"""Offline regression scenarios for wiki binding."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.experiments.wiki import _lessons
from tests.support.wiki_events import failed_attempt, passed_attempt


class VerifiedFixBindingTests(unittest.TestCase):
    def test_lesson_verification_binds_reproduced_checks(self):
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
                                "scenario_id": "check_replay_verified",
                                "status": "failed",
                                "required": True,
                                "error_type": "CandidateCheckRejected",
                                "error": "answer is not an array",
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
                    "verification": {
                        "verdict": "passed",
                        "scenarios": [
                            {
                                "scenario_id": "check_replay_verified",
                                "status": "passed",
                                "required": True,
                                "asset_id": "c_answer_shape",
                                "input_ref": "train:0:0:abcd",
                                "expectation": "verified_must_pass",
                            },
                            {
                                "scenario_id": "check_replay_reject",
                                "status": "passed",
                                "required": True,
                                "asset_id": None,
                                "check_ids": ["c_answer_shape"],
                                "input_ref": "train:0:0:ffff",
                                "expectation": "must_reject",
                            },
                        ],
                    },
                },
            },
        ]
        lessons = _lessons(entries)
        verified = [lesson for lesson in lessons if lesson.get("status") == "admission_verified"]
        self.assertEqual(len(verified), 1, lessons)
        reproduced = verified[0]["verified_fix"].get("reproduced_checks")
        self.assertTrue(reproduced, verified[0]["verified_fix"])
        self.assertTrue(all(p["scenario_id"].startswith("check_replay") for p in reproduced))
        self.assertTrue(all("c_answer_shape" in p["check_ids"] for p in reproduced))


class ArchivedAttributionTests(unittest.TestCase):
    """二次复查 P1-B：归档 loop3 全部 formal/decision 事件必须能形成 ≤35000 字符的
    维护请求并写归因（离线 RecordedClient，零真实模型调用）。"""

    def test_all_archived_travel_attributions_fit_budget(self):
        from darwinagent.experiments.wiki import WikiMaintainer
        from tests.support.clients import LedgerRecordedClient

        root = Path("datasets/travelplanner/runs/wiki_gap_repair_20261005_loop3/train")
        if not root.exists():
            self.skipTest(
                "归档证据目录不在本检出（运行产物不进版本库）；在产生该证据的运行侧本测试为强制项"
            )
        events = [
            json.loads(p.read_text()) for p in sorted((root / "optimization/events").glob("*.json"))
        ]
        targets = [e for e in events if e["kind"] in ("formal", "decision")]
        self.assertGreaterEqual(len(targets), 4)
        for event in targets:
            reply = [
                {
                    "cause": "offline probe attribution",
                    "action": "review bounded facts",
                    "training_ids": event["training_ids"],
                }
            ]
            client = LedgerRecordedClient({"wiki_maintainer": reply})
            with tempfile.TemporaryDirectory() as tmp:
                maintainer = WikiMaintainer(
                    tmp,
                    "offline-attribution",
                    lambda _, client=client: client,
                    RunConfig(protocol_attempts=1),
                    limit=10,
                )
                asyncio.run(maintainer._attribute(event))
                request = Path(tmp) / "optimization" / "maintenance" / f"{event['id']}.request.json"
                self.assertTrue(request.exists(), event["id"])
                payload = json.loads(request.read_text())
                self.assertLessEqual(
                    len(json.dumps(payload, ensure_ascii=False, default=str)), 35000, event["id"]
                )
                out = Path(tmp) / "optimization" / "maintenance" / f"{event['id']}.json"
                self.assertTrue(out.exists(), event["id"])


class VerifiedFixBindingRefTests(unittest.TestCase):
    """二次复查 P2：同名场景不同输入不算复现；结构档通过不能证明语义故障修复。"""

    @staticmethod
    def _lesson_entry(pattern_scenario, error_type, input_ref):
        return {
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
                            "asset_id": "f_tool",
                            "scenario_id": pattern_scenario,
                            "status": "failed",
                            "required": True,
                            "input_ref": input_ref,
                            "error_type": error_type,
                            "error": "boom",
                        }
                    ]
                },
            },
        }

    @staticmethod
    def _passed_entry(rows):
        return {
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
                        "asset_id": "f_tool",
                        "after": {
                            "id": "f_tool",
                            "kind": "F",
                            "content": 'def run(p):\n    return {"rows": [], "truncated": False}\n',
                            "input_contract": {"type": "any"},
                            "output_contract": {"type": "any"},
                            "trial_inputs": [{"x": 1}],
                            "fingerprint": "f" * 64,
                        },
                    }
                ],
                "verification": {"verdict": "passed", "scenarios": rows},
            },
        }

    def test_same_scenario_different_ref_does_not_verify(self):
        from darwinagent.experiments.wiki import _lessons

        entries = [
            self._lesson_entry("replay", "SandboxError", "conv-26:replay:aaaa1111aaaa1111"),
            self._passed_entry(
                [
                    {
                        "asset_id": "f_tool",
                        "scenario_id": "replay",
                        "status": "passed",
                        "required": True,
                        "input_ref": "conv-26:replay:bbbb2222bbbb2222",
                    }
                ]
            ),
        ]
        lessons = _lessons(entries)
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )

    def test_same_ref_does_verify_replay_lesson(self):
        from darwinagent.experiments.wiki import _lessons

        entries = [
            self._lesson_entry("replay", "SandboxError", "conv-26:replay:aaaa1111aaaa1111"),
            self._passed_entry(
                [
                    {
                        "asset_id": "f_tool",
                        "scenario_id": "replay",
                        "status": "passed",
                        "required": True,
                        "input_ref": "conv-26:replay:aaaa1111aaaa1111",
                    }
                ]
            ),
        ]
        lessons = _lessons(entries)
        self.assertTrue(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )

    def test_structure_row_pass_does_not_verify(self):
        from darwinagent.experiments.wiki import _lessons

        entries = [
            self._lesson_entry("answer_0", "CandidateCheckRejected", "conv-26:answer_0"),
            self._passed_entry(
                [
                    {
                        "asset_id": "c_shape",
                        "scenario_id": "answer_0",
                        "status": "passed",
                        "required": True,
                        "expectation": "structure",
                        "ok": False,
                        "input_ref": "conv-26:answer_0",
                    }
                ]
            ),
        ]
        # answer_0 lesson belongs to c_shape; make asset match
        entries[1]["facts"]["asset_changes"][0]["asset_id"] = "c_shape"
        entries[1]["facts"]["asset_changes"][0]["after"]["id"] = "c_shape"
        entries[1]["facts"]["asset_changes"][0]["after"]["kind"] = "C"
        lessons = _lessons(entries)
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )


class AdmissionFixIdentityTests(unittest.TestCase):
    """四次复查反例 B：lesson 验证必须绑定 case＋场景族＋参数/快照 digest＋数据图
    身份。反例证据：docs/diagnostics/wiki-gap-recheck4-20261005/remaining-probes.json
    （case-old 失败、case-new 同参数通过仍产 admission_verified）。"""

    _failed = staticmethod(failed_attempt)

    _passed = staticmethod(passed_attempt)

    DIGEST = "3320bdd2d325b999efdc40189b4d0306caa9e69f59deedfdf14f8cad34c6fb1d"

    def test_cross_case_same_params_does_not_verify(self):
        """反例本体（现状红）：case-old 的 stress 失败不得被 case-new 的同参数
        通过行验证——base/stress 参数来自资产级 trial_inputs，跨 case 必然同
        digest，旧逻辑只看 digest 子串。"""
        from darwinagent.experiments.wiki import _lessons

        lessons = _lessons(
            [
                self._failed(f"case-old:stress:0:{self.DIGEST}"),
                self._passed(
                    [
                        {
                            "asset_id": "f_flight_pair",
                            "scenario_id": "stress",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"case-new:stress:0:{self.DIGEST}",
                        }
                    ]
                ),
            ]
        )
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )

    def test_same_case_cross_scenario_same_params_does_not_verify(self):
        """场景绑定：同 case 同 digest 但不同场景族（stress 失败、base 通过）
        不得验证——场景是绑定要素之一（四审）。"""
        from darwinagent.experiments.wiki import _lessons

        lessons = _lessons(
            [
                self._failed(f"conv-26:stress:0:{self.DIGEST}"),
                self._passed(
                    [
                        {
                            "asset_id": "f_flight_pair",
                            "scenario_id": "base",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"conv-26:base:0:{self.DIGEST}",
                        }
                    ]
                ),
            ]
        )
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )

    def test_graph_digest_mismatch_does_not_verify(self):
        """数据图身份：同 case 同场景同 digest，但验证运行的数据图与失败时不同
        （可重建图场景）→ 旧故障不能算已修复。"""
        from darwinagent.experiments.wiki import _lessons

        g1, g2 = "a" * 63 + "1", "a" * 63 + "2"
        lessons = _lessons(
            [
                self._failed(f"conv-26:stress:0:{self.DIGEST}", graph_digests={"conv-26": g1}),
                self._passed(
                    [
                        {
                            "asset_id": "f_flight_pair",
                            "scenario_id": "stress",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"conv-26:stress:0:{self.DIGEST}",
                        }
                    ],
                    graph_digests={"conv-26": g2},
                ),
            ]
        )
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )
        # 图一致时仍验证（既有用例不回退）
        lessons = _lessons(
            [
                self._failed(f"conv-26:stress:0:{self.DIGEST}", graph_digests={"conv-26": g1}),
                self._passed(
                    [
                        {
                            "asset_id": "f_flight_pair",
                            "scenario_id": "stress",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"conv-26:stress:0:{self.DIGEST}",
                        }
                    ],
                    graph_digests={"conv-26": g1},
                ),
            ]
        )
        self.assertTrue(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )

    def test_same_case_same_scenario_same_digest_verifies(self):
        """守门（既有行为不回退）：同 case＋同场景族＋同 digest＋同图 → 验证成立。"""
        from darwinagent.experiments.wiki import _lessons

        lessons = _lessons(
            [
                self._failed(f"conv-26:stress:0:{self.DIGEST}"),
                self._passed(
                    [
                        {
                            "asset_id": "f_flight_pair",
                            "scenario_id": "stress",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"conv-26:stress:0:{self.DIGEST}",
                        }
                    ]
                ),
            ]
        )
        self.assertTrue(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )

    def test_real_check_replay_ref_shape_verifies(self):
        """真实 check_replay ref 形态（case:question_id:<12hex>，第二段是题号而非
        场景名）：同 case＋同题号＋同快照 digest 的通过行必须验证——场景比对用
        ref 段对 ref 段，不得与行 scenario_id 标签比对（否则真实回放验证被误拒）。"""
        from darwinagent.experiments.wiki import _lessons

        d12 = self.DIGEST[:12]
        lessons = _lessons(
            [
                self._failed(
                    f"conv-26:5:{d12}", scenario="check_replay_verified", asset="c_answer_shape"
                ),
                self._passed(
                    [
                        {
                            "asset_id": "c_answer_shape",
                            "scenario_id": "check_replay_verified",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"conv-26:5:{d12}",
                        }
                    ],
                    asset_id="c_answer_shape",
                    kind="C",
                ),
            ]
        )
        self.assertTrue(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"], lessons
        )
