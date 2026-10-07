"""Offline regression scenarios for admission."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.config import RunConfig
from darwinagent.contracts import (
    AnswerResult,
    CaseInput,
    EvaluationResult,
    QuestionInput,
    RunResult,
    SourceRef,
)
from darwinagent.experiments.runner import ExperimentRunner
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.llm.recorded import RecordedClient


class AbstentionAuditScopeTests(unittest.TestCase):
    def test_agentic_refusal_audit_stays_within_retrieved_evidence(self):
        # 评审#5：融合版拒答审计不得读全图——covers_full_graph=False、证据＝已召回行；
        # 需要补证只能显式调用登记工具（调用与返回都在轨迹里）。
        import asyncio

        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from tests.support.device import review
        from tests.support.graphs import (
            ROOT,
            FakeEmbedder,
            build_snapshot,
            cold_bundle,
            corpus,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            spec = TaskSpec.load(ROOT / "tasks/conversation_memory/task.yaml").with_bundle(
                cold_bundle(root)
            )
            case = CaseInput("conv-x", corpus(), (QuestionInput("q1", "甲在哪年买了游艇？"),))
            replies = {
                "tools": [
                    {"action": "call", "asset_id": "f_semantic", "parameters": {"query": "游艇"}},
                    {"action": "ready"},
                ],
                "answer": [{"status": "abstained", "answer": "记忆中无支持。", "node_ids": []}],
                "review": [review(status="abstained")],
            }
            client = RecordedClient(replies)
            result = asyncio.run(
                Pipeline(
                    client,
                    root / "gen",
                    frozen_snapshot=snapshot,
                    embedder_factory=lambda: FakeEmbedder(),
                ).run(case, spec, RunConfig(protocol_attempts=1))
            )
            self.assertEqual(result.answers[0].status, "abstained")
            audits = []
            for call in client.calls:
                if call.get("role") != "review":
                    continue
                for message in call["messages"]:
                    text = message.get("content", "") if isinstance(message, dict) else str(message)
                    if "refusal_audit" in str(text):
                        import json as _json

                        audits.append(
                            _json.loads(text)
                            if isinstance(text, str) and text.startswith("{")
                            else text
                        )
            self.assertTrue(audits, "拒答审计 review 调用应当存在")
            audits = [a for a in audits if isinstance(a, dict)]
            self.assertTrue(audits)
            n_nodes = json.loads((snapshot / "manifest.json").read_text())["n_nodes"]
            for audit in audits:
                self.assertFalse(audit.get("refusal_audit", {}).get("covers_full_graph", False))
                self.assertEqual(audit.get("refusal_audit", {}).get("mode"), "agentic_retrieved")
                visible = audit.get("candidate", {}).get("visible_evidence", [])
                # 只看已召回行（远小于全图），未召回内容不得借审计通道进入
                self.assertLess(len(visible), n_nodes)


class AdmissionRetryThenSuccessTests(unittest.TestCase):
    def test_stale_fingerprint_feeds_back_and_second_call_admits(self):
        # 评审#2：准入错（指纹回显）回灌提案重试后成功准入——整轮不作废
        import asyncio
        import contextlib
        import io

        from tests.support.recorded_experiment import RecordedExperiment

        class RetryOnceExperiment(RecordedExperiment):
            def __init__(self, root):
                super().__init__(root)
                self.admission_calls = 0
                original = self.revisions.propose

                def patched(*args, **kwargs):
                    self.admission_calls += 1
                    if self.admission_calls == 1:
                        raise ValueError("Stale baseline or asset type change")
                    return original(*args, **kwargs)

                self.revisions.propose = patched

            def _client(self, stage):
                client = super()._client(stage)
                if stage == "R1" and self.stage_clients[stage] == 1:
                    # 首次提案被拒后重试：同一 stage 客户端需要第二份提案输出
                    client.replies["proposal"].append(client.replies["proposal"][0])
                return client

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = RetryOnceExperiment(root)
            from darwinagent.kernel import TaskSpec
            from tests.support.recorded_experiment import TASK

            task_spec = TaskSpec.load(TASK / "task.yaml")
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(runner.run(runner.case.id, task_spec, rounds=1))
            self.assertEqual(runner.admission_calls, 2)  # 第一次拒、第二次成
            self.assertTrue(summary["rounds"][0]["accepted"])
            self.assertEqual(summary["status"], "complete")


class GraphCheckBudgetTests(unittest.TestCase):
    def _registry(self, steps=30000):
        from darwinagent.kernel.checks import CheckRegistry
        from darwinagent.operators.sandbox import Limits

        src = (
            "def check(candidate):\n"
            " issues=[]\n fact=0\n"
            " for n in candidate.get('nodes', []):\n"
            "  if n.get('entity_type')=='原子事实':\n"
            "   fact=fact+1\n"
            "   if not n.get('陈述',''):\n"
            "    issues.append('空陈述')\n"
            " if fact==0:\n"
            "  issues.append('无原子事实')\n"
            " return {'ok': not issues, 'issues': issues}\n"
        )

        class A:
            kind = "C"
            id = "c"
            fingerprint = "f"
            stage = "graph"
            content = src

        class B:
            assets = type("AS", (), {"assets": (A(),)})()
            version = "v"

            def verify(self):
                pass

        return CheckRegistry(B(), Limits(steps, 15.0, 180000))

    def test_budget_scales_with_snapshot(self):
        # 评审①：1100 节点的图检查需 ~40k 步 > function_steps——预算随图规模伸缩后通过，
        # 且记录实际耗用 steps_used / step_budget
        rows = [
            {
                "node_id": f"n{i:06d}",
                "entity_type": "原子事实",
                "陈述": f"事实{i}",
                "source_ids": ["s"],
            }
            for i in range(2200)
        ]
        opinions = self._registry().run("graph", {"nodes": rows, "stage": "graph"})
        self.assertTrue(opinions[0]["ok"])
        self.assertGreater(opinions[0]["steps_used"], 30000)  # 事故的真实量级
        self.assertLessEqual(opinions[0]["steps_used"], opinions[0]["step_budget"])
        no_nodes = self._registry().run("graph", {"stage": "graph"})
        self.assertEqual(no_nodes[0]["step_budget"], 30000)  # 无节点不放大预算

    def test_stage_skip_and_preflight(self):
        import asyncio
        from types import SimpleNamespace

        from darwinagent.experiments import stages as R

        # (a) 图阶段全局失败：不进入分批重试（FakePipeline 只被调用一次）
        class FakeClient:
            async def aclose(self):
                pass

            def ledger_summary(self):
                return {"total_calls": 0}

        _ev = (SourceRef("m", "c", "1"),)
        graph_failed = RunResult(
            "c",
            "i",
            "v",
            (AnswerResult("q1", "execution_error", "", error="x"),),
            0,
            ({"status": "execution_error", "error": "SandboxError: budget"},),
        )
        calls = []

        class OncePipeline:
            def __init__(self, client, work_dir, frozen_snapshot=None, graph_builder=None):
                pass

            async def run(self, case, spec, config):
                calls.append(1)
                return graph_failed

        class C:
            id = "c"
            questions = (QuestionInput("q1", "?"), QuestionInput("q2", "?"))

        with tempfile.TemporaryDirectory() as td:
            runner = ExperimentRunner(
                type("Adapter", (), {"generation_input": staticmethod(lambda i: C())})(),
                lambda c, p: type("E", (), {"evaluate": None})(),
                None,
                RunConfig(protocol_attempts=1),
                None,
                Path(td) / "r",
                client_factory=lambda s: FakeClient(),
            )
            # 图阶段全局失败：断言不进入分批重试（评测异常在此路径上必然发生，宽断言）
            with mock.patch.object(R, "Pipeline", OncePipeline):
                with self.assertRaises(Exception):
                    asyncio.run(
                        runner._stage(
                            "B0", [C()], SimpleNamespace(bundle=SimpleNamespace(version="v"))
                        )
                    )
        self.assertEqual(calls, [1])  # 只跑了一次，没有分批重试


class PreflightStagingTests(unittest.TestCase):
    """评审三：预检失败不残留 candidate 目录；重试修好后正常准入；恢复已有候选也要过预检。"""

    def test_first_preflight_failure_second_attempt_admits(self):
        import asyncio
        import contextlib
        import io

        from darwinagent.kernel import TaskSpec
        from tests.support.recorded_experiment import TASK, RecordedExperiment

        class PreflightFailOnce(RecordedExperiment):
            def __init__(self, root):
                super().__init__(root)
                self.preflight_calls = 0
                real = self._preflight

                def patched(candidate, spec, *args, **kwargs):
                    self.preflight_calls += 1
                    if self.preflight_calls == 1:
                        raise ValueError("候选预检失败: SandboxError: boom")
                    return real(candidate, spec, *args, **kwargs)

                self._preflight = patched

            def _client(self, stage):
                client = super()._client(stage)
                if stage == "R1" and self.stage_clients[stage] == 1:
                    client.replies["proposal"].append(client.replies["proposal"][0])
                return client

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PreflightFailOnce(root)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = asyncio.run(
                    runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=1)
                )
            self.assertTrue(summary["rounds"][0]["accepted"])
            self.assertEqual(runner.preflight_calls, 2)
            # 正式候选目录就位；首次失败的暂存目录保留审计且不阻塞
            self.assertTrue((root / "R1" / "candidate" / "bundle" / "manifest.json").exists())
            self.assertTrue((root / "R1" / ".candidate-attempt-0").exists())
            self.assertNotIn("already exists", json.dumps(summary, ensure_ascii=False))

    def test_resume_revalidates_existing_candidate(self):
        import asyncio
        import contextlib
        import io

        from darwinagent.kernel import TaskSpec
        from tests.support.recorded_experiment import TASK, RecordedExperiment

        class Recording(RecordedExperiment):
            def __init__(self, root):
                super().__init__(root)
                self.preflight_specs = []
                real = self._preflight

                def patched(candidate, spec, *args, **kwargs):
                    self.preflight_specs.append(candidate.version)
                    return real(candidate, spec, *args, **kwargs)

                self._preflight = patched

            def _client(self, stage):
                # 恢复路径没有提案客户端：R1 的第一个客户端就是阶段评测客户端
                if stage == "R1" and self.stage_clients[stage] == 0:
                    from tests.support.clients import LedgerRecordedClient
                    from tests.support.device import client as fx

                    self.stage_clients[stage] += 1
                    c = LedgerRecordedClient({r: list(v) for r, v in fx(self.case).replies.items()})
                    self.created.append(c)
                    return c
                return super()._client(stage)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            from darwinagent.kernel.registration import load_assets

            load_assets(TASK).export(root / "seed")  # 设备任务 bundle，导出目录＝root/'seed'
            runner = Recording(root)
            target = root / "R1" / "candidate" / "bundle"
            target.parent.mkdir(parents=True)
            import shutil

            shutil.copytree(root / "seed", target)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=1))
            self.assertTrue(runner.preflight_specs)  # 恢复路径确实执行了预检


class SmokeThresholdTests(unittest.TestCase):
    """冒烟门槛与 B0 冷门成比例（v10 搬运事故：1/3 低概率 F 契约绊倒≠系统性破绽）：
    ≥2/3 执行错误或 0 有效作答才拒；单题故障由 B0 冷门（≥95% 完成度）吸收。"""

    def test_single_fault_passes_double_fault_rejects(self):
        from dataclasses import dataclass

        import darwinagent.experiments.stages as R
        from darwinagent.experiments.runner import ExperimentRunner

        @dataclass
        class Q:
            id: str
            text: str = "?"
            parameters: dict = None

        @dataclass
        class Case:
            id: str
            questions: tuple

        gate = ExperimentRunner._smoke_gate

        async def fake_pipeline_run(self, case, spec, config):
            answers = []
            for i, q in enumerate(case.questions):
                if i < fail_count:
                    answers.append(
                        AnswerResult(q.id, "execution_error", "", error="SandboxError: x")
                    )
                else:
                    answers.append(AnswerResult(q.id, "abstained", "记忆中无支持", ()))
            from types import SimpleNamespace as NS

            return NS(answers=tuple(answers))

        for fail_count, expect_block in ((3, False), (4, True), (6, True)):

            class _StubClient:
                async def aclose(self):
                    pass

            runner = object.__new__(ExperimentRunner)
            runner._client = lambda stage: _StubClient()
            runner.snapshot_root = None
            runner.smoke_judge = None
            runner.graph_builder = None
            runner.config = RunConfig(protocol_attempts=1)
            case = Case("c", tuple(Q(f"q{i}") for i in range(6)))
            with (
                mock.patch.object(R.Pipeline, "run", fake_pipeline_run),
                mock.patch.object(R, "tempfile", create=True),
            ):
                err = asyncio.run(gate(runner, [case], None))
            self.assertEqual(expect_block, err is not None, f"fail_count={fail_count}: {err}")


class SmokeJudgeContractTests(unittest.TestCase):
    """冒烟判题与正式判题同一入口后的接缝回归（v10 attempt1/2 秒退：字段名笔误
    evaluation_faults 写成 eval_faults，冒烟判题一处崩溃整轮作废）。"""

    def test_smoke_judge_maps_evaluation_result_fields(self):
        import datasets.locomo.evaluator as EV
        import datasets.locomo.run as R

        async def fake_evaluate(self, result, asked=None):
            return EvaluationResult({"original_precise": 2}, 3, 2, 0, 1)

        answers = (AnswerResult("0", "abstained", "x", ()),) * 3
        with mock.patch.object(EV.LocomoEvaluator, "evaluate", fake_evaluate):
            verdict = asyncio.run(R.smoke_judge(None, SimpleNamespace(id="conv-26"), answers))
        self.assertEqual(verdict, {"precise": 2, "completed": 2, "total": 3})


class EvidenceBoundaryTests(unittest.TestCase):
    """证据边界（专家实锤＋用户批准）：作答可引用证据面＝工具返回的行；内部读过未返回
    的行只进 read_node_ids 溯源；返回行里伪造的 node_id（未读过）被剔除。"""

    def test_returned_rows_only_plus_fabrication_guard(self):
        import tempfile

        from darwinagent.experiments.snapshots import attach_vector, load_frozen_graph
        from darwinagent.kernel.functions import FunctionRegistry
        from darwinagent.operators.sandbox import Limits
        from tests.support.graphs import FakeEmbedder, build_snapshot, corpus

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot, _ = build_snapshot(root)
            graph = load_frozen_graph(snapshot, corpus())
            attach_vector(graph, snapshot, embedder_factory=lambda: FakeEmbedder())
            # 读多返回少：nodes 全读 50 行，返回只留 2 行＋1 个伪造 node_id
            src = (
                "def run(params):\n"
                " rows = nodes('原子事实', limit=50)\n"
                " out = [{'node_id': r['node_id'], '陈述': r.get('陈述', '')} for r in rows[:2]]\n"
                " out.append({'node_id': 'nFAKE999', '陈述': '伪造'})\n"
                " return {'rows': out}\n"
            )
            contract = {
                "type": "object",
                "properties": {"rows": {"type": "array"}},
                "additionalProperties": True,
            }
            f = Asset(
                "f_pick",
                "F",
                src,
                {"type": "object"},
                contract,
                ["schema"],
                description="挑两行",
                trial_inputs=({},),
            )
            from tests.support.graphs import cold_bundle

            base = cold_bundle(root / "b")
            keep = [a for a in base.assets.assets if a.kind != "F"]
            bundle = KernelAssets(tuple(keep + [f])).export(root / "b2")
            reg = FunctionRegistry(bundle, Limits(30000, 15.0, 180000))
            result = reg.call("f_pick", {}, graph)
            _caps_rows = graph  # 快照图行集来自 load_frozen_graph 的 DataCapabilities
            from darwinagent.operators.data import DataCapabilities

            all_read = DataCapabilities(graph).rows
            fact_ids = [nid for nid, row in all_read.items() if row["entity_type"] == "原子事实"]
            self.assertEqual(len(result["read_node_ids"]), min(50, len(fact_ids)))
            self.assertLessEqual(len(result["node_ids"]), len(result["read_node_ids"]))
            self.assertNotIn("nFAKE999", result["node_ids"])  # 伪造引用被剔除
            for nid in result["node_ids"]:
                self.assertIn(nid, result["read_node_ids"])  # 可引用⊆真实读取
                self.assertIn(nid, all_read)


class RetryVarianceTests(unittest.TestCase):
    """R5 事故：50 次重试只发生 2 次真实调用——报错字符串相同→提示词相同→LLM 缓存
    返回首次坏补丁。重试提示词必须携带序号（缓存破坏），50 次才是真的 50 次。"""

    def test_admission_error_carries_attempt_number(self):
        import contextlib
        import io

        from darwinagent.experiments.proposal import ProposalGenerator
        from darwinagent.experiments.runner import ADMISSION_ATTEMPTS
        from darwinagent.kernel import TaskSpec
        from tests.support.device import TASK
        from tests.support.recorded_experiment import RecordedExperiment

        errors = []

        async def reject(_generator, *args, **kwargs):
            errors.append(kwargs.get("admission_error"))
            raise ValueError("same invalid proposal")

        with tempfile.TemporaryDirectory() as tmp:
            runner = RecordedExperiment(Path(tmp))
            with (
                mock.patch.object(ProposalGenerator, "propose", reject),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                summary = asyncio.run(
                    runner.run(runner.case.id, TaskSpec.load(TASK / "task.yaml"), rounds=1)
                )
        self.assertEqual(len(errors), ADMISSION_ATTEMPTS)
        self.assertIsNone(errors[0])
        for attempt, error in enumerate(errors[1:], 1):
            self.assertIn(f"[重试 {attempt}/{ADMISSION_ATTEMPTS}]", error)
            self.assertIn("same invalid proposal", error)
        decision = summary["rounds"][0]
        self.assertEqual(decision["status"], "validation_failed")
        self.assertIn(f"[重试 {ADMISSION_ATTEMPTS}/{ADMISSION_ATTEMPTS}]", decision["reasons"][0])
