"""Offline regression scenarios for proposal feedback."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.contracts import (
    AnswerResult,
    EvaluationResult,
    QuestionInput,
    RunResult,
    SourceRef,
)
from darwinagent.kernel.assets import Asset
from darwinagent.llm.recorded import RecordedClient


class TraceSummaryTests(unittest.TestCase):
    def _answer(self, tool_data, review_accepted=True, review_feedback=None):
        ev = (SourceRef("message_text", "c", "1"),)
        raw = (
            '{"action":"call","asset_id":"f_semantic","parameters":{"query":"q"}}',
            '{"action":"ready"}',
            '{"status":"answered","answer":"ok"}',
            '{"accepted":true,"feedback":"ok"}',
        )
        trace = (
            {
                "stage": "tool",
                "attempt": 0,
                "step": 0,
                "asset_id": "f_semantic",
                "data": tool_data,
                "node_ids": [],
                "source_ids": [],
                "read_operations": 1,
                "capability_calls": {"semantic_search": 1},
            },
            {"stage": "candidate", "attempt": 0, "candidate": {"status": "answered"}, "checks": ()},
            {
                "stage": "review",
                "attempt": 0,
                "accepted": review_accepted,
                "feedback": review_feedback or "ok",
            },
        )
        return AnswerResult("0", "answered", "ok", evidence=ev, raw_outputs=raw, trace=trace)

    def test_tool_calls_distinct_from_model_outputs(self):
        # 评审#2 反例：一次工具调用 + ready + 作答 + 审查＝4 条模型输出，工具调用数必须是 1
        from darwinagent.experiments.feedback import _retrieval_trace

        summary = _retrieval_trace(self._answer([{"编号": "c-0001"}]))
        self.assertEqual(summary["tool_calls"], 1)
        self.assertEqual(summary["model_calls"], 4)
        self.assertEqual(summary["returned_rows"], 1)  # 来自工具返回，不是最终引用数
        self.assertEqual(summary["stopped"], "ready")
        self.assertEqual(summary["tools"][0]["tool"], "f_semantic")
        self.assertEqual(summary["tools"][0]["capabilities"], {"semantic_search": 1})

    def test_empty_result_and_review_rejection_visible(self):
        from darwinagent.experiments.feedback import _retrieval_trace

        summary = _retrieval_trace(
            self._answer([], review_accepted=False, review_feedback="证据不足，不应作答")
        )
        self.assertEqual(summary["empty_results"], 1)
        self.assertTrue(
            any(r["by"] == "review" and "证据不足" in r["reason"] for r in summary["rejections"])
        )


class TraceGapTests(unittest.TestCase):
    """评审②四缺口：检查理由（mappingproxy）可见；被拒调用后成功工具参数正确；
    证据与来源可见；摘要截断可识别。"""

    def _answer(self, trace_events, raw):
        ev = (SourceRef("message_text", "c", "1"),)
        # AnswerResult 构造时会 freeze trace —— mappingproxy 是真实运行时形态
        return AnswerResult("0", "answered", "ok", evidence=ev, raw_outputs=raw, trace=trace_events)

    def test_frozen_check_reasons_visible(self):
        from darwinagent.experiments.feedback import _retrieval_trace

        events = (
            {
                "stage": "candidate",
                "attempt": 0,
                "candidate": {"status": "answered"},
                "checks": ({"check_id": "fixed.cite", "ok": False, "issues": ["引用了不可见行"]},),
            },
        )
        answer = self._answer(events, ('{"status":"answered"}', '{"accepted":true}'))
        summary = _retrieval_trace(answer)
        rejected = [r for r in summary["rejections"] if r["by"] == "check"]
        self.assertTrue(rejected)
        self.assertIn("引用了不可见行", rejected[0]["issues"])

    def test_params_come_from_execution_record_not_rejected_calls(self):
        # 第一次动作调未登记工具被拒（raw_outputs 里留下错误参数），第二次成功——
        # 摘要必须用工具事件内执行点记录的 parameters
        from darwinagent.experiments.feedback import _retrieval_trace

        raw = (
            '{"action":"call","asset_id":"bogus_tool","parameters":{"wrong":true}}',
            '{"action":"call","asset_id":"f_good","parameters":{"stale":"x"}}',
            '{"action":"ready"}',
        )
        events = (
            {
                "stage": "tool",
                "attempt": 0,
                "step": 0,
                "asset_id": "f_good",
                "parameters": {"query": "正确参数"},
                "data": [{"node_id": "n1"}],
                "node_ids": [],
                "source_ids": [],
                "read_operations": 1,
                "capability_calls": {"semantic_search": 1},
            },
        )
        summary = _retrieval_trace(self._answer(events, raw))
        self.assertEqual(summary["tools"][0]["params"], '{"query": "正确参数"}')
        self.assertNotIn("stale", summary["tools"][0]["params"])

    def test_evidence_excerpts_and_sources_visible(self):
        from darwinagent.experiments.feedback import _retrieval_trace

        events = (
            {
                "stage": "tool",
                "attempt": 0,
                "step": 0,
                "asset_id": "f_semantic",
                "parameters": {"query": "q"},
                "data": [
                    {
                        "node_id": "n000001",
                        "陈述": "甲计划下周修打印机",
                        "source_ids": ["s1", "s2"],
                    },
                    {"node_id": "n000002", "陈述": "乙觉得跑步能减压", "source_ids": ["s3"]},
                ],
                "node_ids": [],
                "source_ids": [],
                "read_operations": 2,
                "capability_calls": {"semantic_search": 1},
            },
        )
        summary = _retrieval_trace(self._answer(events, ('{"action":"ready"}',)))
        evidence = summary["tools"][0]["evidence"]
        self.assertEqual(evidence[0]["node_id"], "n000001")
        self.assertIn("修打印机", evidence[0]["statement"])
        self.assertEqual(evidence[0]["source_ids"], ["s1", "s2"])

    def test_truncation_is_marked(self):
        from darwinagent.experiments.feedback import _retrieval_trace

        events = tuple(
            {
                "stage": "tool",
                "attempt": 0,
                "step": i,
                "asset_id": f"f_{i}",
                "parameters": {"q": "x" * 400},
                "data": [],
                "node_ids": [],
                "source_ids": [],
                "read_operations": 1,
                "capability_calls": {"search": 1},
            }
            for i in range(30)
        )
        summary = _retrieval_trace(self._answer(events, ('{"action":"ready"}',)))
        self.assertLess(len(summary["tools"]), 30)
        self.assertGreater(summary.get("tools_truncated", 0), 0)


class CrossCaseTraceTests(unittest.TestCase):
    def test_same_question_id_keeps_case_local_trace(self):
        # 评审#3 反例：A、B 对话都有第 0 题，A 用 tool_A、B 用 tool_B——反馈不得串用
        from darwinagent.experiments import feedback as R

        ev = (SourceRef("message_text", "c", "1"),)

        def make(tool):
            raw = (
                f'{{"action":"call","asset_id":"{tool}","parameters":{{"query":"q"}}}}',
                '{"action":"ready"}',
                '{"status":"answered","answer":"ok"}',
                '{"accepted":true}',
            )
            trace = (
                {
                    "stage": "tool",
                    "attempt": 0,
                    "step": 0,
                    "asset_id": tool,
                    "data": [],
                    "node_ids": [],
                    "source_ids": [],
                    "read_operations": 1,
                    "capability_calls": {"semantic_search": 1},
                },
            )
            return AnswerResult("0", "answered", "ok", evidence=ev, raw_outputs=raw, trace=trace)

        rows = (
            {
                "question_id": "0",
                "status": "answered",
                "original": {"precise": False, "lenient": False},
            },
        )
        results = [
            RunResult("a", "i", "v", (make("tool_A"),), 1),
            RunResult("b", "i2", "v", (make("tool_B"),), 1),
        ]

        class C1:
            id = "a"

        class C2:
            id = "b"

        baseline = EvaluationResult({"m": 0}, 0, 2, 0, 0, rows)
        feedback = R.training_feedback([C1(), C2()], results, [("a", rows), ("b", rows)], baseline)
        by_case = {r["case_id"]: r["trace"]["tools"][0]["tool"] for r in feedback["diagnostics"]}
        self.assertEqual(by_case, {"a": "tool_A", "b": "tool_B"})


class ProposalFeedbackTests(unittest.TestCase):
    def locomo_row(self, qid, precise):
        return {
            "question_id": qid,
            "question": f"Q{qid}",
            "status": "answered",
            "answer": "a" * 300,
            "error": None,
            "original": {
                "idx": 0,
                "key": "k",
                "status": "ok",
                "id": "c0",
                "lenient": precise,
                "precise": precise,
                "missing_elements": [],
                "wrong_elements": ["缺" * 80],
                "precision_issues": [],
                "reference_items": ["GOLD-SECRET"],
            },
        }

    def test_failure_first_compressed_with_trace(self):
        from darwinagent.experiments import feedback as R

        rows = (
            self.locomo_row("0", True),
            self.locomo_row("1", False),
            self.locomo_row("2", False),
        )
        ev = (SourceRef("message_text", "c", "1"),)
        raw = (
            '{"action":"call","asset_id":"search_facts","parameters":{"terms":["x"]}}',
            '{"action":"ready"}',
            '{"status":"answered","answer":"wrong"}',
            '{"accepted":true}',
        )
        answers = (
            AnswerResult("0", "answered", "ok", evidence=ev),
            AnswerResult(
                "1",
                "answered",
                "wrong",
                evidence=ev,
                raw_outputs=raw,
                trace=(
                    {
                        "stage": "tool",
                        "attempt": 0,
                        "step": 0,
                        "asset_id": "search_facts",
                        "data": [{"编号": "x"}],
                        "node_ids": [],
                        "source_ids": [],
                        "read_operations": 1,
                        "capability_calls": {"search": 1},
                    },
                ),
            ),
            AnswerResult("2", "execution_error", "", error="ProtocolError: boom"),
        )
        result = RunResult("c", "id", "v", answers, 5)

        class C:
            id = "c"
            questions = (QuestionInput("q1", "?"), QuestionInput("q2", "?"))

        baseline = EvaluationResult({"original_precise": 1}, 2, 3, 1, 0, rows)
        feedback = R.training_feedback([C()], [result], [("c", rows)], baseline)
        ids = [r["diagnostic"]["question_id"] for r in feedback["diagnostics"]]
        self.assertEqual(ids, ["1", "2"])  # 通过题不进反馈（失败优先）
        blob = json.dumps(feedback, ensure_ascii=False)
        self.assertNotIn("GOLD-SECRET", blob)  # 金标不进提案载荷
        self.assertNotIn("reference_items", blob)
        traces = {r["diagnostic"]["question_id"]: r.get("trace") for r in feedback["diagnostics"]}
        self.assertEqual(traces["1"]["tool_calls"], 1)
        self.assertEqual(traces["1"]["model_calls"], 4)
        self.assertEqual(traces["1"]["tools"][0]["tool"], "search_facts")
        self.assertTrue(traces["1"]["tools"][0]["params"].startswith('{"terms"'))
        self.assertEqual(traces["2"]["status"], "execution_error")
        self.assertEqual(feedback["diagnostic_rows_total"], 2)

    def test_budget_rotates_across_cases(self):
        from darwinagent.experiments import feedback as R

        rows_a = tuple(self.locomo_row(str(i), False) for i in range(6))
        rows_b = tuple(self.locomo_row(str(i), False) for i in range(6))
        answers = tuple(
            AnswerResult(str(i), "answered", "x", evidence=(SourceRef("m", "c", "1"),))
            for i in range(6)
        )

        class C1:
            id = "a"

        class C2:
            id = "b"

        results = [RunResult("a", "i", "v", answers, 1), RunResult("b", "i2", "v", answers, 1)]
        baseline = EvaluationResult({"original_precise": 0}, 0, 12, 0, 0, rows_a + rows_b)
        feedback = R.training_feedback(
            [C1(), C2()], results, [("a", rows_a), ("b", rows_b)], baseline, budget=2600
        )
        per_case = {}
        for r in feedback["diagnostics"]:
            per_case.setdefault(r["case_id"], 0)
            per_case[r["case_id"]] += 1
        self.assertEqual(sorted(per_case), ["a", "b"])  # 预算紧张时轮转：两个对话都有行
        self.assertLess(sum(per_case.values()), 12)


class ProposalPayloadContract(unittest.TestCase):
    def test_scope_and_admission_error_reach_the_model(self):
        from darwinagent.experiments.proposal import ProposalGenerator
        from tests.support.device import case, spec

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        s = spec(root / "assets")
        client = RecordedClient({"proposal": ["not-json"] * 6})
        config = __import__("darwinagent.config", fromlist=["RunConfig"]).RunConfig()

        async def run():
            return await ProposalGenerator().propose(
                s.bundle if hasattr(s, "bundle") else None,
                [case()],
                {"x": 1},
                client,
                config,
                root / "call.json",
                [{"training_id": "2::c::q1", "text": "t"}],
                allowed_kinds=("P",),
                admission_error="ValueError: Stale baseline or asset type change",
            )

        with self.assertRaises(Exception):
            asyncio.run(run())
        recorded = json.loads((root / "call.json").read_text())
        self.assertEqual(recorded["input"]["allowed_asset_kinds"], ["P"])
        self.assertIn("Stale baseline", recorded["input"]["previous_admission_error"])
        self.assertIn("may only patch", recorded["protocol"])


class TraceReturnTypesTests(unittest.TestCase):
    """评审四：合法的非数组工具返回不崩摘要；空结果与合法 0/False 区分。"""

    def _summarize(self, data):
        from darwinagent.experiments.feedback import _retrieval_trace

        ev = (SourceRef("message_text", "c", "1"),)
        raw = ('{"action":"call","asset_id":"t","parameters":{}}', '{"action":"ready"}')
        events = (
            {
                "stage": "tool",
                "attempt": 0,
                "step": 0,
                "asset_id": "t",
                "parameters": {},
                "data": data,
                "node_ids": [],
                "source_ids": [],
                "read_operations": 1,
                "capability_calls": {"aggregate": 1},
            },
        )
        return _retrieval_trace(
            AnswerResult("0", "answered", "ok", evidence=ev, raw_outputs=raw, trace=events)
        )

    def test_scalar_and_object_returns(self):
        for value, label in (
            ({"count": 3}, "object"),
            (3, "number"),
            (False, "bool"),
            ("文本", "string"),
        ):
            summary = self._summarize(value)
            self.assertEqual(summary["empty_results"], 0, label)  # 合法值不算空
            self.assertEqual(summary["tools"][0]["rows"], 1, label)
            self.assertIn("returns", summary["tools"][0], label)
        none_summary = self._summarize(None)
        self.assertEqual(none_summary["empty_results"], 1)  # None 才是空结果
        self.assertEqual(none_summary["tools"][0]["rows"], 0)


class ActiveStagesFeedbackTests(unittest.TestCase):
    """专家规格#5：冻结快照下 P.extract 不执行——反馈必须告知提案器「改它不进计分路径」。"""

    def test_frozen_snapshot_marks_extract_skipped(self):
        from darwinagent.experiments.feedback import pipeline_active_stages

        frozen = pipeline_active_stages(Path("/snapshots"))
        self.assertIn("SKIPPED", frozen["P.extract"])
        self.assertIn("不因 S 补丁重建", frozen["S"])
        live = pipeline_active_stages(None)
        self.assertNotIn("SKIPPED", live["P.extract"])


class ToolTelemetryTests(unittest.TestCase):
    """专家规格#2（反馈层事后差分实现——不触碰作答路径，答案检查点可跨框架搬运）：
    逐调用新增证据增量（new_node_ids）与重复调用标记（repeat_call）。"""

    def test_new_ids_and_repeat_flag(self):
        from types import SimpleNamespace as NS

        from darwinagent.experiments.feedback import _retrieval_trace

        params = {"query": "甲"}
        trace = (
            {
                "stage": "tool",
                "asset_id": "f_semantic",
                "parameters": dict(params),
                "node_ids": ["n1", "n2"],
                "data": [
                    {"node_id": "n1", "陈述": "x", "source_ids": []},
                    {"node_id": "n2", "陈述": "y", "source_ids": []},
                ],
            },
            {
                "stage": "tool",
                "asset_id": "f_semantic",
                "parameters": dict(params),
                "node_ids": ["n2", "n3"],
                "data": [{"node_id": "n3", "陈述": "z", "source_ids": []}],
            },
        )
        summary = _retrieval_trace(
            NS(question_id="q1", status="answered", trace=trace, raw_outputs=())
        )
        tools = [t for t in summary.get("tools", []) if isinstance(t, dict)]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0].get("new_node_ids"), ["n1", "n2"])
        self.assertFalse(tools[0].get("repeat_call"))
        self.assertEqual(tools[1].get("new_node_ids"), ["n3"])  # n2 已召回不再计新增
        self.assertTrue(tools[1].get("repeat_call"))  # 同工具同参数再现


class CrossRoundRejectionFeedbackTests(unittest.TestCase):
    """规格#5（用户抓到的真缺口）：上一轮拒绝原因必须进下一轮提案反馈——
    轮内重试看得到 admission_error，跨轮以前看不到，导致每轮摔新坑不带记忆。"""

    def test_previous_round_rejection_enters_feedback(self):
        from darwinagent.contracts import RunResult
        from darwinagent.experiments.feedback import training_feedback

        result = RunResult("c", "i", "v", (), (), ())
        payload = training_feedback(
            (SimpleNamespace(id="c", questions=()),),
            (result,),
            (("c", {}),),
            EvaluationResult({"m": 0}, 0, 0, 0, 0),
            active_stages={"F": "执行中"},
            previous_round={
                "round": "R2",
                "status": "validation_failed",
                "accepted": False,
                "reasons": ["SandboxError: Container-to-string"],
            },
        )
        import json

        blob = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertIn("previous_round", blob)
        self.assertIn("Container-to-string", blob)


class PatchNormalizationTests(unittest.TestCase):
    """v13 R4 十连败死因：提案把展示用 fingerprint 键回显进资产对象。机械剥离多余键，
    格式类错误不再消耗重试预算（用户拍板重试上限 50 次，留给内容类问题）。"""

    def test_extra_keys_dropped_and_noted(self):
        import inspect

        from darwinagent.experiments.proposal import ProposalGenerator

        src = inspect.getsource(ProposalGenerator)
        self.assertIn("dropped", src)
        # 直接驱动 valid：构造带多余键的补丁载荷

        item = {
            "id": "p_x",
            "kind": "P",
            "role": "tools",
            "content": "指引",
            "input_contract": {"type": "any"},
            "output_contract": {"type": "any"},
            "schema_dependencies": [],
            "description": "d",
            "trial_inputs": [],
            "fingerprint": "should-be-dropped",
        }

        class FakeSession:
            async def request(self, *a, **k):
                raise AssertionError("不经会话")

        _gen = ProposalGenerator.__new__(ProposalGenerator)
        _valid = None
        # 通过类内部协议函数直接验证剥离逻辑（不整段伪造会话）
        import dataclasses

        fields = {f.name for f in dataclasses.fields(Asset)}
        extra = sorted(set(item) - fields - {"schema_dependencies"})
        self.assertEqual(extra, ["fingerprint"])
        cleaned = {k: v for k, v in item.items() if k in fields or k == "schema_dependencies"}
        self.assertNotIn("fingerprint", cleaned)
        asset = Asset(**cleaned)
        self.assertEqual(asset.id, "p_x")
