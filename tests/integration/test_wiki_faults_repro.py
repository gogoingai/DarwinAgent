"""Travel 归档故障复现（红→绿）：动态图准入缺口与冻结容器误判。

证据出处（只读归档，2026-10-05 wiki 验证）：
- F 空值契约：wiki_loop_compat_20261005_case0_evalfix R1 与 _runtimecontract B0
  （graph.failure.json: "ValueError: tool.result.inbound/outbound: expected object"）。
- C 冻结容器误判：evalfix B0 三轮 'answer is not an array'（合法 JSON 数组候选被
  isinstance(ans,list) 误杀）；attribution_replay attempt-2 只修数组漏对象
  （'day N is not an object'）；attempt-4 为验证过的修复形态。
夹具 = 归档资产/图/合法候选的逐字拷贝（tests/fixtures/travel_faults/）。
"""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from networkx import freeze

from darwinagent.config import RunConfig
from darwinagent.contracts import GraphResult
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.experiments.admission import AdmissionError, admit_candidate
from darwinagent.kernel import KernelBundle, TaskSpec
from darwinagent.kernel.checks import CheckRegistry
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kernel.revision import AssetPatch, AssetRevisionService, training_id
from darwinagent.kernel.validation import capability_names
from darwinagent.kg.graph import load_graph

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "travel_faults"
TASK_YAML = Path("tasks/travel_planning/task.yaml")


def travel_case():
    from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef

    raw = json.loads((FIXTURES / "case_input.json").read_text())
    blocks = tuple(
        CorpusBlock(SourceRef(**b["source"]), b["text"], b["metadata"]) for b in raw["corpus"]
    )
    return CaseInput(raw["id"], blocks, tuple(QuestionInput(**q) for q in raw["questions"]))


def travel_graph(case):
    return GraphResult(
        freeze(load_graph(FIXTURES / "graph.json")), {b.source.id: b for b in case.corpus}
    )


def base_bundle():
    return KernelBundle(FIXTURES / "b0_assets")


def legal_candidate():
    return json.loads((FIXTURES / "legal_candidate.json").read_text())


def candidate_bundle(base, replacements, root):
    """替换 {asset_id: 新 content} 构造候选版本（走正式 AssetRevisionService 校验）。"""
    patches = [
        AssetPatch(
            replace(a, content=replacements[a.id]),
            a.fingerprint,
            "Red-repro repair of archived 2026-10-05 travel faults",
            (training_id("train:0", "0"),),
        )
        for a in base.assets.assets
        if a.id in replacements
    ]
    service = AssetRevisionService()
    return service.propose(
        base,
        patches,
        Path(root) / "candidate",
        (training_id("train:0", "0"),),
        (),
        tuple({a.kind for a in base.assets.assets if a.id in replacements}),
        (),
    )


FIXED_C = (FIXTURES / "c_answer_shape_fixed.py").read_text()
# 归档 attempt-4 修复漏了弃答形态（structured_answer=None 时回落到字符串答案仍报
# not-an-array——四标签回放未覆盖 abstain）。COMPLETE_C = 归档修复＋弃答分支，
# 代表「结构检查完全合法」的候选 C；归档原版自身的这一残余缺陷由下方断言如实记录。
COMPLETE_C = FIXED_C.replace(
    "def check(candidate):\n    issues = []\n    ans = None\n",
    "def check(candidate):\n    issues = []\n    ans = None\n"
    "    if candidate.get('status') == 'abstained':\n"
    "        return {'ok': True, 'issues': []}\n",
)
# 任务正确的语义 C（审查 P1 修复后的电池口径）：容器正确＋弃答合法＋语义检查齐全
# （天序 1..N、城市非空）。它对契约占位实例（空城市）的拒绝是合法语义拒绝。
IDEAL_C = COMPLETE_C.replace(
    "    return {'ok': len(issues) == 0, 'issues': issues}",
    "    numbers = [d.get('days') for d in ans]\n"
    "    if numbers != list(range(1, len(ans) + 1)):\n"
    "        issues.append('days sequence must be 1..N')\n"
    "    if any(not (d.get('current_city') or '') for d in ans):\n"
    "        issues.append('current_city must be non-empty')\n"
    "    return {'ok': len(issues) == 0, 'issues': issues}",
)
ATTEMPT2_C = (FIXTURES / "c_answer_shape_attempt2.py").read_text()
# 测试自拟的 F 修复形态：无匹配航班时返回空对象而非 None（契约 outbound/inbound
# 声明 type:object 且必填——归档事故的机器可判定修法，等价于归档维护器建议）。
FIXED_F = (
    (FIXTURES / "b0_assets/assets/F/f_flight_pair.py")
    .read_text()
    .replace("out = {'outbound': None, 'inbound': None,", "out = {'outbound': {}, 'inbound': {},")
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
        case = travel_case()
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
                    answer_counterexamples=spec.answer_counterexamples,
                    answer_examples=spec.answer_examples,
                )
                return json.loads(report_path.read_text())
            except AdmissionError as exc:
                return exc.report

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


# 官方语义的接地 C：在容器正确＋弃答合法之上，按官方约束语义检查内容——
# 每日三餐非空（官方 is_not_absent）、餐食/景点/住宿字符串包含当日城市之一
# （官方 current_city 规则）、引用名称必须能在证据行中找到（官方沙盒存在性；
# 官方条目格式为 "Name, City"，逗号前为名称）。
# 四次复查反例 A 的红态：此类合理 C 必须接受任务正例夹具（旧夹具 day1 三餐
# 全空＋实体虚构＋证据取活案例图首行，被其误拒）。
GROUND_C = COMPLETE_C.replace(
    "    return {'ok': len(issues) == 0, 'issues': issues}",
    "    ev = list(candidate.get('evidence') or []) + list(candidate.get('visible_evidence') or [])\n"
    "    names = {(r.get('name') or '') for r in ev if r.get('entity_type') in "
    "('restaurant', 'accommodation', 'attraction')}\n"
    "    flights = {str(r.get('flight_number') or '') for r in ev if r.get('entity_type') == 'flight'}\n"
    "    for d in ans:\n"
    "        for meal in ('breakfast', 'lunch', 'dinner'):\n"
    "            if not (d.get(meal) or ''):\n"
    "                issues.append('Meals must be present on non-transit days')\n"
    "        city = str(d.get('current_city') or '')\n"
    "        cities = [c.strip() for c in city.replace('from ', '').split(' to ')] if city else []\n"
    "        for field in ('breakfast', 'lunch', 'dinner', 'attraction', 'accommodation'):\n"
    "            value = str(d.get(field) or '')\n"
    "            for part in [p for p in value.split(';') if p.strip()]:\n"
    "                if cities and not any(c in part for c in cities):\n"
    "                    issues.append(field + ' not in day cities')\n"
    "                ref = part.split(', ')[0] if ', ' in part else part\n"
    "                if ref not in names:\n"
    "                    issues.append(field + ' references unknown entity')\n"
    "        transport = str(d.get('transportation') or '')\n"
    "        if 'Flight Number: ' in transport:\n"
    "            no = transport.split('Flight Number: ')[1].split(',')[0]\n"
    "            if no not in flights:\n"
    "                issues.append('flight not in evidence')\n"
    "    return {'ok': len(issues) == 0, 'issues': issues}",
)


class FourthReviewFixtureTests(TravelFaultReproductionTests):
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
        from darwinagent.experiments.runner import stress_trial_samples

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


class DynamicTrialGraphTests(unittest.TestCase):
    """图供给身份纪律：未触碰 S/P.extract 复用已采纳真图；触碰必须重抽——
    拿旧图证明新候选安全=身份失配。试验图缓存跨候选复用（确定性产物，非请求重放）。"""

    def _runner(self, root, case):
        async def _close():
            pass

        return ExperimentRunner(
            type("Adapter", (), {"generation_input": lambda _, ident: case})(),
            lambda transport, path: None,
            None,
            RunConfig(protocol_attempts=1),
            AdoptionPolicy("final", ()),
            root,
            dynamic_trial=True,
            client_factory=lambda stage: type("Client", (), {"aclose": staticmethod(_close)})(),
        )

    def _stage_b0(self, root, base):
        generation = root / "B0" / "generation" / "train:0"
        generation.mkdir(parents=True, exist_ok=True)
        (generation / "graph.json").write_text((FIXTURES / "graph.json").read_text())
        from darwinagent.runtime.artifacts import digest

        payload = json.loads((generation / "graph.json").read_text())
        (generation / "graph.complete.json").write_text(json.dumps({"digest": digest(payload)}))
        (root / "B0" / "stage.json").write_text(
            json.dumps({"stage": "B0", "asset_version": base.version})
        )

    def test_c_only_patch_reuses_adopted_graph_s_patch_forces_rebuild(self):
        import tempfile
        from unittest import mock

        case = travel_case()
        base = base_bundle()
        schema_asset = next(a for a in base.assets.assets if a.kind == "S")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        self._stage_b0(root, base)

        class CountingAgent:
            calls = 0

            def __init__(self, *args):
                pass

            async def extract_entities(self, corpus):
                CountingAgent.calls += 1
                return travel_graph(travel_case())

        runner = self._runner(root, case)
        with mock.patch("darwinagent.agents.ExtractionAgent", CountingAgent):
            c_only = candidate_bundle(base, {"c_answer_shape": COMPLETE_C}, str(root / "c1"))
            asyncio.run(runner._dynamic_trial_graphs(c_only, [case]))
            self.assertEqual(CountingAgent.calls, 0, "未触碰 S/P.extract 必须复用已采纳图")

            tampered = replace(
                schema_asset, content=schema_asset.content + "\n# candidate schema edit\n"
            )
            from darwinagent.kernel.revision import AssetPatch

            s_patch = AssetRevisionService().propose(
                base,
                [
                    AssetPatch(
                        tampered, schema_asset.fingerprint, "S 扩展", (training_id("train:0", "0"),)
                    )
                ],
                root / "c2",
                (training_id("train:0", "0"),),
                (),
                ("S",),
                (),
            )
            asyncio.run(runner._dynamic_trial_graphs(s_patch, [case]))
            self.assertEqual(CountingAgent.calls, 1, "触碰 S 必须用候选资产重抽真图")

    def test_extract_cache_reused_across_candidates_sharing_extraction_face(self):
        import tempfile
        from unittest import mock

        case = travel_case()
        base = base_bundle()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)

        class CountingAgent:
            calls = 0

            def __init__(self, *args):
                pass

            async def extract_entities(self, corpus):
                CountingAgent.calls += 1
                return travel_graph(travel_case())

        runner = self._runner(root, case)
        two = [
            candidate_bundle(base, {"c_answer_shape": COMPLETE_C}, str(root / f"c{i}"))
            for i in range(2)
        ]
        with mock.patch("darwinagent.agents.ExtractionAgent", CountingAgent):
            for bundle in two:
                asyncio.run(runner._dynamic_trial_graphs(bundle, [case]))
        self.assertEqual(CountingAgent.calls, 1, "同 (case,S,P.extract,config,transport) 只抽一次")


class RealFaultAdmissionTests(unittest.TestCase):
    """二次复查 P1-C：真实故障 F（conv-30 f_filter_facts limit=500 预算耗尽）必须被
    准入在正式答题前拦截——数值放大×宽过滤组合样本＋历史失败实参回放，同预算不改。"""

    REAL_PARAMS = {"subject": "乔恩", "fact_type": "", "date_prefix": "", "limit": 500}
    MC_B0 = Path("datasets/locomo/runs/wiki_gap_repair_20261005_mc/train/B0/assets")

    def setUp(self):
        required = (
            self.MC_B0 / "manifest.json",
            Path("datasets/locomo/snapshots/gvtest_v1/conv-30/manifest.json"),
            Path("datasets/locomo/snapshots/gvtest_v1/conv-30/graph.json"),
        )
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            self.skipTest("External archived evidence missing: " + ", ".join(missing))

    FIXED_FILTER_F = """def run(params):
    filters = {}
    subject = params.get('subject', '')
    if subject != '':
        filters['主体'] = subject
    ftype = params.get('fact_type', '')
    if ftype != '':
        filters['类型'] = ftype
    date_prefix = params.get('date_prefix', '')
    limit = params.get('limit', 50)
    if isinstance(limit, int) is False or limit < 1 or limit > 500:
        limit = 50
    rows = list(nodes(entity_type='原子事实', filters=filters, limit=limit))
    if date_prefix != '':
        rows = [r for r in rows if str(dict(r).get('日期') or '').startswith(date_prefix)]
    cap = 60
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    out = []
    for r in rows:
        d = dict(r)
        for f in ['编号', '陈述', '主体', '类型', '日期', '日期原文', '主题', '出处', 'node_id', 'entity_type']:
            if d.get(f) is None:
                d[f] = ''
        if d.get('source_ids') is None:
            d['source_ids'] = []
        out.append(d)
    return {'rows': out, 'count': len(out), 'scanned': len(rows), 'truncated': truncated,
            'note': 'rows capped before per-row normalization with truncated disclosure'}
"""

    @classmethod
    def _case_graph(cls):
        from darwinagent.experiments.snapshots import load_frozen_graph
        from datasets.locomo.adapter import LocomoAdapter

        case = LocomoAdapter(Path("datasets/locomo/data/locomo10_zh.json")).generation_input(
            "conv-30"
        )
        graph = load_frozen_graph(Path("datasets/locomo/snapshots/gvtest_v1/conv-30"), case.corpus)
        return case, graph

    def _admit_mc(self, content_override=None):
        from datasets.locomo.run import TASK_DIR

        base = KernelBundle(self.MC_B0)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        replacements = {}
        if content_override is not None:
            replacements["f_filter_facts"] = content_override
        bundle = candidate_bundle(base, replacements, holder.name) if replacements else base
        case, graph = self._case_graph()
        spec = TaskSpec.load(TASK_DIR / "task.yaml")
        report_path = Path(holder.name) / "admission.json"
        try:
            admit_candidate(
                bundle,
                [case],
                {case.id: graph},
                RunConfig(),
                capability_names(spec.retrieval_floor),
                report_path,
                replay_inputs=(("conv-30", "f_filter_facts", self.REAL_PARAMS),),
            )
            return json.loads(report_path.read_text())
        except AdmissionError as exc:
            return exc.report

    def test_original_fault_f_blocked_at_admission(self):
        report = self._admit_mc()
        budget = [
            s
            for s in report["scenarios"]
            if s.get("asset_id") == "f_filter_facts"
            and s.get("status") == "failed"
            and "budget" in str(s.get("error", ""))
        ]
        self.assertTrue(
            budget,
            [s.get("scenario_id") for s in report["scenarios"] if s.get("status") == "failed"],
        )

    def test_fixed_f_passes_same_budget_with_truncation(self):
        report = self._admit_mc(self.FIXED_FILTER_F)
        rows = [s for s in report["scenarios"] if s.get("asset_id") == "f_filter_facts"]
        self.assertTrue(rows)
        self.assertTrue(
            all(s.get("status") in ("passed", "skipped") for s in rows),
            [s for s in rows if s.get("status") == "failed"],
        )

    def test_stress_combo_covers_real_fault_shape(self):
        from darwinagent.experiments.runner import stress_trial_samples

        _, graph = self._case_graph()
        out = stress_trial_samples(
            [{"subject": "乔恩", "fact_type": "事件", "date_prefix": "2023-05", "limit": 20}], graph
        )
        combo = {"subject": "乔恩", "fact_type": "", "date_prefix": "", "limit": 500}
        self.assertTrue(any(p == combo for p in out if isinstance(p, dict)), out[:6])


if __name__ == "__main__":
    unittest.main()
