"""Offline regression scenarios for check replay."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.experiments.admission import AdmissionError, admit_candidate
from darwinagent.experiments.recovery import (
    _check_snapshot_expectation,
    _prior_failed_check_snapshots,
    promote_verified_check_replay,
)
from darwinagent.experiments.wiki_lessons import _lessons
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.validation import capability_names
from darwinagent.runtime.artifacts import digest
from tests.support.checkpoints import (
    TRAVEL_CONTRACT,
    candidate_event,
    legal_snapshot,
    write_checkpoint,
)
from tests.support.travel_faults import (
    COMPLETE_C,
    FIXED_F,
    FIXTURES,
    TASK_YAML,
    base_bundle,
    candidate_bundle,
    legal_candidate,
    travel_case,
    travel_graph,
)


class CheckSnapshotClassificationTests(unittest.TestCase):
    def test_machine_decidable_expectations(self):
        legal = legal_snapshot()
        malformed = {**legal, "answer": "not a JSON array", "structured_answer": None}
        violating = {**legal, "structured_answer": ["not an object"], "answer": '["not an object"]'}
        abstained = {
            **legal,
            "status": "abstained",
            "answer": "无法回答",
            "structured_answer": None,
            "node_ids": [],
        }
        empty = {**legal, "answer": "", "structured_answer": None, "node_ids": []}
        self.assertEqual(_check_snapshot_expectation(legal, TRAVEL_CONTRACT)[0], "informational")
        self.assertEqual(_check_snapshot_expectation(malformed, TRAVEL_CONTRACT)[0], "must_reject")
        self.assertEqual(_check_snapshot_expectation(violating, TRAVEL_CONTRACT)[0], "must_reject")
        self.assertEqual(
            _check_snapshot_expectation(abstained, TRAVEL_CONTRACT)[0], "informational"
        )
        self.assertEqual(_check_snapshot_expectation(empty, TRAVEL_CONTRACT)[0], "must_reject")

    def test_scanner_reads_checkpoints_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_checkpoint(root, "B0", "train:0", "0", [candidate_event(legal_snapshot())])
            write_checkpoint(root, "R1", "train:0", "0", [candidate_event(legal_snapshot())])
            write_checkpoint(
                root,
                "R2",
                "train:0",
                "0",
                [
                    {
                        "stage": "candidate",
                        "candidate": {"status": "answered", "answer": "x", "node_ids": []},
                        "checks": [
                            {"check_id": "c_answer_shape", "ok": False, "issues": ["old style"]}
                        ],
                    }
                ],
            )
            rows = _prior_failed_check_snapshots(root, TRAVEL_CONTRACT)
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["case_id"], "train:0")
            self.assertEqual(rows[0]["expectation"], "informational")
            self.assertEqual(rows[0]["check_ids"], ["c_answer_shape"])

    def test_promotion_binds_verified_rows_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_checkpoint(root, "B0", "train:0", "0", [candidate_event(legal_snapshot())])
            snapshot_digest = digest(legal_snapshot())
            promote_verified_check_replay(
                root,
                [
                    {
                        "snapshot_digest": snapshot_digest,
                        "check_ids": ["c_answer_shape"],
                        "verified_by": "travel probe 2026-10-05",
                        "provenance": {
                            "old_fingerprint": "039af3c0",
                            "new_fingerprint": "12bd5517",
                            "malformed_rejected": True,
                        },
                    }
                ],
            )
            promote_verified_check_replay(
                root,
                [
                    {
                        "snapshot_digest": snapshot_digest,
                        "check_ids": ["c_answer_shape"],
                        "verified_by": "again",
                    }
                ],
            )
            rows = _prior_failed_check_snapshots(root, TRAVEL_CONTRACT)
            self.assertEqual(rows[0]["expectation"], "verified_must_pass")
            store = json.loads((root / "optimization" / "check-replay-verified.json").read_text())
            self.assertEqual(len(store["rows"]), 1)


class CheckReplayAdmissionTests(unittest.TestCase):
    def _rows_root(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def test_must_reject_row_gates_and_passes_correct_c(self):
        root = self._rows_root()
        malformed = {**legal_snapshot(), "answer": "not a JSON array", "structured_answer": None}
        write_checkpoint(root, "B0", "train:0", "0", [candidate_event(malformed)])
        rows = _prior_failed_check_snapshots(root, TRAVEL_CONTRACT)
        self.assertEqual(rows[0]["expectation"], "must_reject")
        case = travel_case()
        bundle = self._patched({"c_answer_shape": COMPLETE_C, "f_flight_pair": None})
        report = self._admit_with_rows(bundle, case, rows)
        reject_rows = [
            s for s in report["scenarios"] if s.get("scenario_id") == "check_replay_reject"
        ]
        self.assertEqual([s["status"] for s in reject_rows], ["passed"], reject_rows)

    def test_verified_row_blocks_misjudging_c_and_passes_fixed_c(self):
        root = self._rows_root()
        write_checkpoint(root, "B0", "train:0", "0", [candidate_event(legal_snapshot())])
        promote_verified_check_replay(
            root,
            [
                {
                    "snapshot_digest": digest(legal_snapshot()),
                    "check_ids": ["c_answer_shape"],
                    "verified_by": "probe",
                    "provenance": {"malformed_rejected": True},
                }
            ],
        )
        rows = _prior_failed_check_snapshots(root, TRAVEL_CONTRACT)
        case = travel_case()
        old_c = (FIXTURES / "b0_assets/assets/C/c_answer_shape.py").read_text()
        misjudging = self._patched({"c_answer_shape": old_c, "f_flight_pair": None})
        report = self._admit_with_rows(misjudging, case, rows)
        self.assertEqual(report["verdict"], "failed")
        blocked = [
            s
            for s in report["scenarios"]
            if s.get("scenario_id") == "check_replay_verified" and s.get("required", True)
        ]
        self.assertTrue(blocked and all(s["status"] == "failed" for s in blocked), blocked)

        fixed = self._patched({"c_answer_shape": COMPLETE_C, "f_flight_pair": None})
        report = self._admit_with_rows(fixed, case, rows)
        verified = [
            s
            for s in report["scenarios"]
            if s.get("scenario_id") == "check_replay_verified" and s.get("required", True)
        ]
        self.assertTrue(verified and all(s["status"] == "passed" for s in verified), verified)
        self.assertEqual(report["verdict"], "passed")

    def test_informational_rows_record_but_do_not_gate(self):
        root = self._rows_root()
        write_checkpoint(root, "B0", "train:0", "0", [candidate_event(legal_snapshot())])
        rows = _prior_failed_check_snapshots(root, TRAVEL_CONTRACT)
        self.assertEqual(rows[0]["expectation"], "informational")
        case = travel_case()
        # 语义上从严的 C（合法快照仍拒——电池之外的历史快照不强制放行）
        strict = self._patched(
            {
                "c_answer_shape": COMPLETE_C.replace(
                    "return {'ok': True, 'issues': []}\n", "return {'ok': True, 'issues': []}\n"
                ),
                "f_flight_pair": None,
            }
        )
        report = self._admit_with_rows(strict, case, rows)
        info = [s for s in report["scenarios"] if s.get("scenario_id") == "check_replay_info"]
        self.assertTrue(info)
        self.assertTrue(all(not s.get("required", True) for s in info))
        self.assertEqual(report["verdict"], "passed")

    def _patched(self, replacements):
        active = {k: v for k, v in replacements.items() if v is not None}
        active.setdefault("f_flight_pair", FIXED_F)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return candidate_bundle(base_bundle(), active, holder.name)

    def _admit_with_rows(self, bundle, case, rows):
        spec = TaskSpec.load(TASK_YAML, bundle)
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "admission.json"
            try:
                admit_candidate(
                    bundle,
                    [case],
                    {case.id: travel_graph(case)},
                    RunConfig(),
                    capability_names(spec.retrieval_floor),
                    report_path,
                    answer_contract=spec.answer_contract,
                    replay_checks=rows,
                )
                return json.loads(report_path.read_text())
            except AdmissionError as exc:
                return exc.report


class AnswerSnapshotPersistenceTests(unittest.TestCase):
    def test_answer_agent_persists_snapshot_on_check_rejection(self):
        """归档事故路径：合法 JSON 数组候选被旧 C 误杀直至重试耗尽——检查快照必须
        随轨迹留存，扫描器必须能从写出的检查点把它变成回放行。"""
        from darwinagent.agents.answer import AnswerAgent

        case = travel_case()
        question = case.questions[0]
        candidate = legal_candidate()

        class StubClient:
            """tools: 先真调一次 f_city_rows（真图执行），再 ready；answer: 从载荷
            visible_evidence 取 node_ids 回合法归档候选——C 误杀路径的真实形态。"""

            def __init__(self):
                self.tool_turn = 0

            async def chat(self, role, messages, **kwargs):
                if role == RunConfig().tools_role:
                    self.tool_turn += 1
                    if self.tool_turn == 1:
                        content = json.dumps(
                            {
                                "action": "call",
                                "asset_id": "f_city_rows",
                                "parameters": {
                                    "table": "restaurants",
                                    "city": "Rockford",
                                    "limit": 5,
                                },
                            }
                        )
                    else:
                        content = json.dumps({"action": "ready"})
                else:
                    payload = json.loads(messages[-1]["content"])
                    ids = [r["node_id"] for r in payload.get("visible_evidence", ())][:5]
                    content = json.dumps(
                        {
                            "status": candidate["status"],
                            "answer": candidate["answer"],
                            "node_ids": ids,
                        }
                    )
                return type("Reply", (), {"content": content})()

            async def aclose(self):
                pass

        spec = TaskSpec.load(TASK_YAML, base_bundle())
        config = RunConfig(protocol_attempts=1, answer_attempts=2)
        from darwinagent.kernel.execution import KernelRuntime

        runtime = KernelRuntime(base_bundle(), config)
        agent = AnswerAgent(runtime, StubClient(), config, spec, "repro")
        result = asyncio.run(agent.answer(question, travel_graph(case)))
        events = [e for e in result.trace if e.get("stage") == "candidate"]
        self.assertTrue(events)
        self.assertTrue(all("check_snapshot" in e for e in events))
        snap = events[0]["check_snapshot"]
        self.assertIsInstance(snap["structured_answer"], (list, tuple))  # plain() 的 JSON 形态
        self.assertEqual(_check_snapshot_expectation(snap, TRAVEL_CONTRACT)[0], "informational")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_checkpoint(root, "B0", case.id, question.id, list(result.to_dict()["trace"]))
            rows = _prior_failed_check_snapshots(root, TRAVEL_CONTRACT)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["expectation"], "informational")
            self.assertEqual(rows[0]["issues"], ["answer is not an array"])


class CounterexampleReplayTests(unittest.TestCase):
    """三次复查三项修复的验收：正例自洽、全场景输入绑定、摘要保故障事实。"""

    def test_params_consistent_semantic_c_passes_and_misfit_fails(self):
        """正例自洽（三次复查 P1）：参数一致＋非转移日三餐规则的语义 C 过电池；
        同一 C 对「旧行程＋不匹配参数」判拒——夹具不会被错装。"""
        from tests.support.travel_faults import FIXED_F

        params_c = (
            "def check(candidate):\n"
            '    if candidate.get("status") == "abstained":\n'
            '        return {"ok": True, "issues": []}\n'
            '    ans = candidate.get("structured_answer")\n'
            "    if not isinstance(ans, (list, tuple)):\n"
            '        return {"ok": False, "issues": ["answer is not an array"]}\n'
            '    p = candidate.get("parameters") or {}\n'
            '    org = p.get("org", "")\n'
            '    dest = p.get("dest", "")\n'
            "    issues = []\n"
            '    if p.get("days") and len(ans) != p.get("days"):\n'
            '        issues.append("trip length must equal days param")\n'
            "    for i, d in enumerate(ans):\n"
            "        if not isinstance(d, dict):\n"
            "            d = dict(d)\n"
            '        if d.get("days") != i + 1:\n'
            '            issues.append("days must be 1..N")\n'
            "    if ans:\n"
            '        c0 = str(dict(ans[0]).get("current_city") or "")\n'
            '        cs0 = [x.strip() for x in c0.replace("from ", "").split(" to ")] if c0 else []\n'
            "        if not cs0 or cs0[0] != org:\n"
            '            issues.append("first day must start at org")\n'
            "    if ans:\n"
            '        cn = str(dict(ans[-1]).get("current_city") or "")\n'
            '        csn = [x.strip() for x in cn.replace("from ", "").split(" to ")] if cn else []\n'
            "        if not csn or csn[-1] != org:\n"
            '            issues.append("last day must close the trip at org")\n'
            "    for i, d in enumerate(ans):\n"
            "        if not isinstance(d, dict):\n"
            "            d = dict(d)\n"
            '        if i > 0 and not (d.get("lunch") or d.get("dinner")):\n'
            '            issues.append("non-transfer day needs a meal")\n'
            '    return {"ok": not issues, "issues": issues}\n'
        )
        from tests.support.travel_faults import admit_travel_bundle

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        bundle = candidate_bundle(
            base_bundle(), {"c_answer_shape": params_c, "f_flight_pair": FIXED_F}, holder.name
        )
        report = admit_travel_bundle(bundle)
        failed = [
            s
            for s in report["scenarios"]
            if s.get("status") == "failed" and s.get("required", True)
        ]
        self.assertEqual(failed, [], failed)

        # 旧行程（3 天 Rockford）＋夹具参数（2 天 Rockford→Springfield）＝语义错配，
        # 该 C 必须拒绝——正例不会被错装成其他题的答案。
        spec = TaskSpec.load(TASK_YAML)
        fixture = spec.answer_examples[0]
        graph = travel_graph(travel_case())
        from darwinagent.kernel.checks import counterexample_snapshot

        snapshot = counterexample_snapshot(
            {
                **fixture,
                "candidate": {
                    "status": "answered",
                    "answer": json.dumps(
                        json.loads(legal_candidate()["answer"]), ensure_ascii=False
                    ),
                    "node_ids": [
                        next(
                            iter(
                                __import__(
                                    "darwinagent.kernel.functions", fromlist=["DataCapabilities"]
                                )
                                .DataCapabilities(graph)
                                .rows
                            )
                        )
                    ],
                },
            },
            [
                {
                    "node_id": next(
                        iter(
                            __import__(
                                "darwinagent.kernel.functions", fromlist=["DataCapabilities"]
                            )
                            .DataCapabilities(graph)
                            .rows
                        )
                    )
                }
            ],
            fixture["question"],
            fixture["parameters"],
        )
        from darwinagent.kernel.execution import KernelRuntime

        runtime = KernelRuntime(bundle, RunConfig())
        _, opinions = runtime.check_candidate(
            type(
                "Q",
                (),
                {"text": fixture["question"], "parameters": fixture["parameters"], "id": "x"},
            )(),
            snapshot,
            graph,
            set(snapshot["node_ids"]),
        )
        self.assertFalse(next(o for o in opinions if o["check_id"] == "c_answer_shape")["ok"])

    def test_stress_input_mismatch_does_not_verify(self):
        """三次复查 P2：数据相关场景（stress/base）绑定原输入 digest——
        A→B 失败、只证 A→C 成功不得标已修复。"""

        def failed(scenario, ref):
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
                                "asset_id": "f_rel",
                                "scenario_id": scenario,
                                "status": "failed",
                                "required": True,
                                "input_ref": ref,
                                "error_type": "SandboxError",
                                "error": "budget",
                            }
                        ]
                    },
                },
            }

        def passed(rows):
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
                            "asset_id": "f_rel",
                            "after": {
                                "id": "f_rel",
                                "kind": "F",
                                "content": 'def run(p):\n    return {"rows": [], "truncated": False}\n',
                                "input_contract": {"type": "any"},
                                "output_contract": {"type": "any"},
                                "trial_inputs": [{"a": 1}],
                                "fingerprint": "f" * 64,
                            },
                        }
                    ],
                    "verification": {"verdict": "passed", "scenarios": rows},
                },
            }

        origin_digest = "aaaa1111aaaa1111"
        # 不同参数成功（stress 场景另一 digest）→ 不验证
        lessons = _lessons(
            [
                failed("stress", f"conv-26:stress:{origin_digest}"),
                passed(
                    [
                        {
                            "asset_id": "f_rel",
                            "scenario_id": "stress",
                            "status": "passed",
                            "required": True,
                            "input_ref": "conv-26:stress:cccc3333cccc3333",
                        }
                    ]
                ),
            ]
        )
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"]
        )
        # 原参数在 base 场景成功（跨场景同 digest）→ 四次复查「绑定场景」后不再
        # 验证（翻转三审断言：stress 失败只能由 stress 族通过行验证；见
        # FourthReviewBindingTests.test_same_case_cross_scenario_same_params_does_not_verify）
        lessons = _lessons(
            [
                failed("stress", f"conv-26:stress:{origin_digest}"),
                passed(
                    [
                        {
                            "asset_id": "f_rel",
                            "scenario_id": "base",
                            "status": "passed",
                            "required": True,
                            "input_ref": f"conv-26:base:{origin_digest}",
                        }
                    ]
                ),
            ]
        )
        self.assertFalse(
            [lesson for lesson in lessons if lesson.get("status") == "admission_verified"]
        )

    def test_loop6_failure_summary_keeps_rejection_reason(self):
        """三次复查 P1：真实 loop6 R1 attempt 的 C 拒绝理由（issues）必须在维护
        请求的场景行里保留——维护器要能读到反复拒绝的原因。"""
        from darwinagent.experiments.wiki import WikiMaintainer
        from tests.support.clients import LedgerRecordedClient

        root = Path(
            "datasets/travelplanner/runs/wiki_gap_repair_20261005_loop6/train/optimization/events"
        )
        if not root.exists():
            self.skipTest(
                "归档证据目录不在本检出（运行产物不进版本库）；在产生该证据的运行侧本测试为强制项"
            )
        source = next(p for p in sorted(root.glob("025aa34a*.json")))
        event = json.loads(source.read_text())
        reply = [{"cause": "offline", "action": "ok", "training_ids": event["training_ids"]}]
        with tempfile.TemporaryDirectory() as tmp:
            m = WikiMaintainer(
                tmp,
                "offline3",
                lambda _: LedgerRecordedClient({"wiki_maintainer": reply}),
                RunConfig(protocol_attempts=1),
                limit=10,
            )
            asyncio.run(m._attribute(event))
            request = json.loads(
                (
                    Path(tmp) / "optimization" / "maintenance" / f"{event['id']}.request.json"
                ).read_text()
            )
            rows = (
                (request["event"]["facts"].get("admission") or {}).get("scenarios")
                or request["event"]["facts"].get("scenarios")
                or []
            )
            self.assertTrue(rows)
            rejected = [r for r in rows if "structured_answer" in str(r.get("issues", ""))]
            self.assertTrue(rejected, rows)

    def test_tight_budget_keeps_nested_failure_facts(self):
        """5000 字符级预算下，candidate 事件的 checks[].check_id/ok/issues/步数、
        candidate_summary.json_type、observation 预算事实仍然保留。"""
        from darwinagent.experiments.wiki_evidence import _compress_training_evidence

        facts = {
            "training_examples": [
                {
                    "question_id": "0",
                    "parameters": {"org": "A"},
                    "generated_answer": "x" * 4000,
                    "source_text": [{"text": "y" * 2000}],
                    "trace": [
                        {
                            "stage": "candidate",
                            "attempt": 0,
                            "candidate": {"status": "answered"},
                            "checks": [
                                {
                                    "check_id": "c_answer_structure",
                                    "ok": False,
                                    "issues": [
                                        "structured_answer is missing or not parseable JSON"
                                    ],
                                    "steps_used": 45,
                                    "step_budget": 30000,
                                }
                            ],
                            "candidate_summary": {"json_type": "list", "answer": "z" * 900},
                            "observation": {"steps_used": 45, "step_budget": 30000},
                        }
                    ],
                }
            ]
        }
        _compress_training_evidence(facts, budget=1200)
        import json as _json

        self.assertLessEqual(len(_json.dumps(facts, ensure_ascii=False)), 1400)
        event = facts["training_examples"][0]["trace"][0]
        check = event["checks"][0]
        self.assertEqual(check["check_id"], "c_answer_structure")
        self.assertIs(check["ok"], False)
        self.assertEqual(check["issues"], ["structured_answer is missing or not parseable JSON"])
        self.assertEqual(check["steps_used"], 45)
        self.assertEqual(event["candidate_summary"]["json_type"], "list")
        self.assertEqual(event["observation"]["steps_used"], 45)
