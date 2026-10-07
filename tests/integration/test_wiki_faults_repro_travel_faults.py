"""Offline regression scenarios for travel faults."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.experiments.admission import AdmissionError
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.checks import CheckRegistry
from darwinagent.kernel.execution import KernelRuntime
from tests.support.travel_faults import (
    ATTEMPT2_C,
    COMPLETE_C,
    FIXED_C,
    FIXED_F,
    FIXTURES,
    GROUND_C,
    IDEAL_C,
    TASK_YAML,
    admit_travel_bundle,
    base_bundle,
    candidate_bundle,
    legal_candidate,
    travel_case,
    travel_graph,
)


class TravelFaultReproductionTests(unittest.TestCase):
    def test_t1_null_contract_violation_reproduces(self):
        """复现事实：空字符串压力参数触发归档同款空值契约违例（逐字）。"""
        case = travel_case()
        runtime = KernelRuntime(base_bundle(), RunConfig())
        with self.assertRaises(ValueError) as ctx:
            runtime.functions.call(
                "f_flight_pair",
                {"org": "", "dest": "", "date_from": "", "date_to": ""},
                travel_graph(case),
            )
        self.assertIn("tool.result.", str(ctx.exception))
        self.assertIn("expected object", str(ctx.exception))

    def test_t2_old_c_misjudges_archived_legal_candidate(self):
        """复现事实：旧 C 对合法 JSON 数组候选报 'answer is not an array'。"""
        case = travel_case()
        candidate = legal_candidate()
        self.assertEqual(candidate["status"], "answered")
        self.assertEqual(type(json.loads(candidate["answer"])).__name__, "list")
        runtime = KernelRuntime(base_bundle(), RunConfig())
        _, opinions = runtime.check_candidate(
            case.questions[0], candidate, travel_graph(case), set(candidate["node_ids"])
        )
        shape = next(o for o in opinions if o["check_id"] == "c_answer_shape")
        self.assertFalse(shape["ok"])
        self.assertIn("answer is not an array", shape["issues"])

    def _patched_bundle(self, replacements):
        """候选目录须存活到断言结束：临时目录用 addCleanup 保活。"""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return candidate_bundle(base_bundle(), replacements, holder.name)

    def test_t1_dynamic_preflight_intercepts_broken_bundle(self):
        """缺口①红测：动态图任务（无快照/试验图）的候选必须过真图准入——
        含空值契约 F 的候选在准入层被拦并留报告，而不是漏进正式计分。"""
        case = travel_case()
        base = base_bundle()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "B0" / "generation" / "train:0"
            generation.mkdir(parents=True)
            (generation / "graph.json").write_text((FIXTURES / "graph.json").read_text())
            payload = json.loads((generation / "graph.json").read_text())
            from darwinagent.runtime.artifacts import digest

            (generation / "graph.complete.json").write_text(json.dumps({"digest": digest(payload)}))
            (root / "B0" / "stage.json").write_text(
                json.dumps({"stage": "B0", "asset_version": base.version})
            )
            runner = ExperimentRunner(
                type("Adapter", (), {"generation_input": lambda _, ident: case})(),
                lambda transport, path: None,
                None,
                RunConfig(protocol_attempts=1),
                AdoptionPolicy("final", ()),
                root,
                dynamic_trial=True,
            )
            candidate = self._patched_bundle({"c_answer_shape": FIXED_C})
            with self.assertRaises(AdmissionError) as ctx:
                asyncio.run(runner._preflight(candidate, TaskSpec.load(TASK_YAML), cases=[case]))
            scenarios = [
                s for s in ctx.exception.report["scenarios"] if s.get("status") == "failed"
            ]
            self.assertTrue(
                any("tool.result." in str(s.get("error", "")) for s in scenarios), scenarios
            )
            # 真图绑定：报告必须带 graph_digests（复用分支加载的 B0 图）
            self.assertTrue(ctx.exception.report.get("graph_digests"))

    def _admit(self, bundle, extra_checks=False):
        return admit_travel_bundle(bundle, extra_checks=extra_checks)

    def test_battery_ideal_c_passes_and_semantic_rejection_is_legal(self):
        """合法候选通过结构检查（2026-10-05 审查 P1 口径）：容器正确＋语义检查齐全
        （天序 1..N、城市非空、拒畸形、拒任务反例、受弃答）的 C 过全电池——
        它对契约占位实例（days=[1,1]）的语义拒绝合法，结构行只验证可执行。"""
        ideal = self._patched_bundle({"c_answer_shape": IDEAL_C, "f_flight_pair": FIXED_F})
        report = self._admit(ideal)
        failed = [
            s
            for s in report["scenarios"]
            if s.get("status") == "failed" and s.get("required", True)
        ]
        self.assertEqual(failed, [], failed)
        self.assertEqual(report["verdict"], "passed")
        # 占位实例被语义拒绝（ok=False）但结构行通过——结构合法≠语义正确，不得逼删检查
        struct = [
            s
            for s in report["scenarios"]
            if str(s.get("scenario_id", "")).startswith("answer_") and s.get("status") == "passed"
        ]
        self.assertTrue(any(s.get("ok") is False for s in struct), struct)

    def test_battery_rejects_gutted_and_abstain_broken_c(self):
        """守门不放松的两条新路径：删掉语义检查的 C（attempt-6 形态）被任务反例行
        拦下；拒弃答形态的 C（归档旧版）被 abstain 必过行拦下。"""
        gutted = self._patched_bundle({"c_answer_shape": COMPLETE_C, "f_flight_pair": FIXED_F})
        report = self._admit(gutted)
        self.assertEqual(report["verdict"], "failed")
        cx = [
            s
            for s in report["scenarios"]
            if str(s.get("scenario_id", "")).startswith("answer_cx_") and s.get("required", True)
        ]
        self.assertTrue(cx and all(s["status"] == "failed" for s in cx), cx)

        old_c = self._patched_bundle(
            {
                "c_answer_shape": (FIXTURES / "b0_assets/assets/C/c_answer_shape.py").read_text(),
                "f_flight_pair": FIXED_F,
            }
        )
        report = self._admit(old_c)
        abstain = [s for s in report["scenarios"] if s.get("scenario_id") == "answer_2"]
        self.assertTrue(
            any(
                s["status"] == "failed"
                and any("not an array" in i for i in (s.get("issues") or []))
                for s in abstain
            ),
            abstain,
        )

    def test_battery_blocks_reject_all_and_container_bug_c(self):
        """二次复查 P1 反例：只接受弃答、拒绝所有 answered 的 C 与冻结容器误判 C
        （弃答正确但 isinstance(ans,list)）都必须在正式答题前被 answer_ex 真实正例
        行拦下。"""
        abstain_only = (
            "def check(candidate):\n"
            '    if candidate.get("status") == "abstained":\n'
            '        return {"ok": True, "issues": []}\n'
            '    return {"ok": False, "issues": ["answered rejected"]}\n'
        )
        for label, content in (
            ("abstain_only", abstain_only),
            (
                "container_bug",
                COMPLETE_C.replace("isinstance(ans, (list, tuple))", "isinstance(ans, list)"),
            ),
        ):
            with self.subTest(variant=label):
                bundle = self._patched_bundle({"c_answer_shape": content, "f_flight_pair": FIXED_F})
                report = self._admit(bundle)
                self.assertEqual(report["verdict"], "failed")
                ex = [
                    s
                    for s in report["scenarios"]
                    if str(s.get("scenario_id", "")).startswith("answer_ex_")
                    and s.get("required", True)
                ]
                self.assertTrue(ex and all(s["status"] == "failed" for s in ex), ex)

    def test_verified_replay_blocks_misjudging_c_beyond_battery(self):
        """验证回放层（缺口②）的行级证据与电池层并存：同一容器误判 C 在
        answer_ex 行失败（本测试），check_replay_verified 行的独立拦截由
        test_wiki_check_replay 套件行级断言覆盖。"""
        buggy = COMPLETE_C.replace("isinstance(ans, (list, tuple))", "isinstance(ans, list)")
        bundle = self._patched_bundle({"c_answer_shape": buggy, "f_flight_pair": FIXED_F})
        report = self._admit(bundle)
        self.assertEqual(report["verdict"], "failed")
        failed_rows = [
            s.get("scenario_id")
            for s in report["scenarios"]
            if s.get("status") == "failed" and s.get("required", True)
        ]
        self.assertIn("answer_ex_two_day_round_trip", failed_rows)


class TravelFixtureSemanticsTests(TravelFaultReproductionTests):
    """四次复查反例 A：任务正例夹具必须（1）过官方约束校验、（2）被官方语义的
    接地 C 接受（must_pass 行成立）。旧夹具（Rockford→Springfield 单向、day1 三餐
    全空、Springfield 实体全部虚构、证据取活案例图首行）两者皆红——修复=新自洽
    夹具＋夹具自有证据行进入快照，不放松 C。"""

    @staticmethod
    def _fixture():
        import yaml as _yaml

        ex = _yaml.safe_load(Path(TASK_YAML).read_text())["answer_examples"][0]
        return ex, json.loads(ex["candidate"]["answer"])

    def test_official_constraints_validate_fixture(self):
        """夹具行程经官方 commonsense+hard 约束校验全部通过（防漂移守卫：
        夹具数据若偏离官方库/约束语义，本测试先红）。"""
        import subprocess
        import sys
        import tempfile

        from datasets.travelplanner.adapter import TravelPlannerAdapter

        ex, plan = self._fixture()
        query = {
            "org": ex["parameters"]["org"],
            "dest": ex["parameters"]["dest"],
            "days": ex["parameters"]["days"],
            "visiting_city_number": 1,
            "local_constraint": {
                "house rule": None,
                "cuisine": None,
                "room type": None,
                "transportation": None,
            },
            "budget": 2000,
            "people_number": 1,
        }
        tp_root = TravelPlannerAdapter().tp_root
        if not (tp_root / "evaluation").is_dir():
            self.skipTest(
                "External evidence: requires third_party/TravelPlanner official evaluator and database"
            )
        with tempfile.TemporaryDirectory() as tmp:
            inp, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
            inp.write_text(json.dumps({"queries": [query], "plans": [plan]}))
            worker = Path("datasets/travelplanner/pipeline/eval/_worker.py").resolve()
            subprocess.run(
                [sys.executable, str(worker), str(inp), str(out)],
                cwd=tp_root / "evaluation",
                check=True,
                capture_output=True,
                timeout=180,
            )
            result = json.loads(out.read_text())["per_query"][0]
        self.assertIsNone(result.get("error"), result)
        cs = result["commonsense"]
        self.assertTrue(cs, result)
        for key, value in cs.items():
            if isinstance(value, list) and value:
                self.assertTrue(value[0], f"官方 cs 约束 {key} 未过: {value}")
        hc = result.get("hard")
        if hc:
            for key, value in hc.items():
                # None＝该约束不适用（如 local_constraint 为 null 的档位）
                if isinstance(value, list) and value and value[0] is not None:
                    self.assertTrue(value[0], f"官方 hard 约束 {key} 未过: {value}")

    def test_grounded_official_semantics_c_accepts_fixture(self):
        """红→绿：官方语义接地 C（三餐/城市/证据存在性/航班在证据中）必须接受
        新夹具——现状红（快照证据取活案例图首行，夹具证据行未进快照）。"""
        ideal = self._patched_bundle({"c_answer_shape": GROUND_C, "f_flight_pair": FIXED_F})
        report = self._admit(ideal)
        failed = [
            s
            for s in report["scenarios"]
            if s.get("status") == "failed" and s.get("required", True)
        ]
        self.assertEqual(failed, [], failed)
        self.assertEqual(report["verdict"], "passed")

    def test_grounded_c_rejects_ungrounded_and_mismatch(self):
        """接地 C 的否决面：旧 Springfield 形态（虚构实体、空餐）与参数错配
        （夹具问题＋他人行程/虚构航班/虚构餐厅）都必须被拒——修复不得以放松 C
        为代价。证据行用夹具自带行集（构造口径与电池一致）。"""
        from darwinagent.kernel.checks import counterexample_snapshot

        ex, _ = self._fixture()
        old_style = [
            {
                "days": 1,
                "current_city": "Rockford",
                "transportation": "Flight F100 from Rockford to Springfield",
                "breakfast": "",
                "lunch": "",
                "dinner": "",
                "attraction": "",
                "accommodation": "Private Room in Springfield",
            },
            {
                "days": 2,
                "current_city": "Springfield",
                "transportation": "",
                "breakfast": "Breakfast at Springfield Diner",
                "lunch": "Lunch at Springfield Cafe",
                "dinner": "Dinner at Springfield Grill",
                "attraction": "Springfield Museum",
                "accommodation": "Private Room in Springfield",
            },
        ]
        broken = json.loads(json.dumps(json.loads(ex["candidate"]["answer"])))
        broken[1]["transportation"] = "Flight Number: F9999999, From: Rockford to St. Petersburg"
        broken[1]["breakfast"] = "Nowhere Diner (Rockford)"
        for label, candidate in (
            ("old_springfield", {"status": "answered", "answer": json.dumps(old_style)}),
            ("mismatch_itinerary", {"status": "answered", "answer": json.dumps(broken)}),
        ):
            with self.subTest(variant=label):
                snapshot = counterexample_snapshot(
                    {**ex, "candidate": candidate},
                    ex["evidence_rows"],
                    ex["question"],
                    ex["parameters"],
                )
                registry = CheckRegistry(
                    self._patched_bundle({"c_answer_shape": GROUND_C, "f_flight_pair": FIXED_F})
                )
                rows = [
                    r for r in registry.run("answer", snapshot) if r["check_id"] == "c_answer_shape"
                ]
                self.assertTrue(rows, label)
                self.assertFalse(rows[0]["ok"], (label, rows[0].get("issues")))

    def test_archived_fix_residual_abstain_gap_is_exposed(self):
        """如实记录：归档 attempt-4 修复对弃答形态仍误杀（回落字符串→not-an-array）。
        该残余缺陷由契约电池的 abstain 形态当场暴露——历史四标签回放没测过弃答。"""
        _base = base_bundle()
        attempt4 = self._patched_bundle({"c_answer_shape": FIXED_C, "f_flight_pair": FIXED_F})
        report = self._admit(attempt4)
        abstain_rows = [s for s in report["scenarios"] if s.get("scenario_id") == "answer_2"]
        self.assertTrue(
            any(
                s["status"] == "failed"
                and any("not an array" in i for i in (s.get("issues") or []))
                for s in abstain_rows
            ),
            abstain_rows,
        )

    def test_t3_attempt2_day_object_misjudgment_remains_a_fact(self):
        """复现事实保留（审查 P1 后口径更新）：attempt-2 的 day 级冻结对象误判不再
        被占位实例电池拦（语义拒绝合法化后属可执行意见），电池对它现在的拦截点是
        弃答必过行；day 级误判的兜底＝真实候选的验证回放（见
        test_container_bug_c_passes_battery_but_verified_replay_blocks）。"""
        attempt2 = self._patched_bundle({"c_answer_shape": ATTEMPT2_C, "f_flight_pair": FIXED_F})
        report = self._admit(attempt2)
        struct = [
            s
            for s in report["scenarios"]
            if str(s.get("scenario_id", "")).startswith("answer_") and s.get("required", True)
        ]
        self.assertTrue(any(s["status"] == "failed" for s in struct), struct)  # 弃答行拦下

    def test_t4_malformed_rejected_by_all_variants(self):
        """守门不放松：非数组文本答案被旧版/部分修复版/修复版 C 一致拒绝。"""
        case = travel_case()
        graph = travel_graph(case)
        malformed = {**legal_candidate(), "answer": "not a JSON array"}
        for label, content in (
            ("old", (FIXTURES / "b0_assets/assets/C/c_answer_shape.py").read_text()),
            ("attempt2", ATTEMPT2_C),
            ("fixed", FIXED_C),
        ):
            with self.subTest(variant=label):
                bundle = self._patched_bundle({"c_answer_shape": content})
                runtime = KernelRuntime(bundle, RunConfig())
                _, opinions = runtime.check_candidate(
                    case.questions[0], malformed, graph, set(malformed["node_ids"])
                )
                shape = next(o for o in opinions if o["check_id"] == "c_answer_shape")
                self.assertFalse(shape["ok"], shape)

    def test_t5_unregistered_names_rejected(self):
        """沙箱白名单：C 代码使用未注册名（hasattr）在静态准入即被拒
        （AssetRevisionService.propose 内的 validate_bundle 就会拦，轮不到执行）。"""
        from darwinagent.operators.sandbox import SandboxError

        with self.assertRaises(SandboxError):
            self._patched_bundle(
                {
                    "c_answer_shape": "def check(candidate):\n"
                    '    if hasattr(candidate, "answer"):\n'
                    "        return {'ok': True, 'issues': []}\n"
                    "    return {'ok': False, 'issues': ['x']}\n"
                }
            )
