"""Offline regression scenarios for tool replay."""

import asyncio
import json
import tempfile
import unittest

from darwinagent.agents.answer import AnswerAgent
from darwinagent.config import RunConfig
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kg.graph import load_graph
from tests.support.travel_faults import (
    FIXED_C,
    FIXED_F,
    FIXTURES,
    TASK_YAML,
    base_bundle,
    candidate_bundle,
    legal_candidate,
    travel_case,
)


class ToolParamRejectEntersFeedback(unittest.TestCase):
    """A：未声明参数 → tool_call_reject 反馈（含 allowed_fields）→ 修正后正常作答。"""

    def test_undeclared_param_feedbacks_then_publishes(self):
        case = travel_case()
        question = case.questions[0]
        graph = type(
            "G",
            (),
            {
                "graph": load_graph(FIXTURES / "graph.json"),
                "sources": {b.source.id: b for b in case.corpus},
            },
        )()
        seen_feedback = []

        class StubClient:
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
                                    "主题": "餐饮",
                                },
                            }
                        )
                    else:
                        # 第二次必须已收到带 allowed_fields 的拒绝反馈并改对参数
                        payload = json.loads(messages[-1]["content"])
                        rejects = [
                            f for f in payload.get("feedback") or [] if "tool_call_reject" in f
                        ]
                        if self.tool_turn == 2:
                            assert rejects, "重试请求必须携带 tool_call_reject 反馈"
                            assert "allowed_fields" in rejects[0]["tool_call_reject"]
                            assert "主题" not in rejects[0]["tool_call_reject"]["allowed_fields"]
                        seen_feedback.extend(rejects)
                        content = json.dumps(
                            {
                                "action": "call",
                                "asset_id": "f_city_rows",
                                "parameters": {
                                    "table": "restaurants",
                                    "city": "Rockford",
                                    "limit": 50,
                                },
                            }
                        )
                    return type("Reply", (), {"content": content})()
                if role == RunConfig().review_role:
                    return type(
                        "Reply",
                        (),
                        {
                            "content": json.dumps(
                                {
                                    "accepted": True,
                                    "supported": True,
                                    "subject_correct": True,
                                    "consistent": True,
                                    "complete": True,
                                    "abstention_valid": True,
                                    "feedback": "ok",
                                }
                            )
                        },
                    )()
                payload = json.loads(messages[-1]["content"])
                rows = payload.get("tool_results") or []
                ids = sorted({r for row in rows for r in (row.get("node_ids") or ())})
                return type(
                    "Reply",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "status": "answered",
                                "answer": legal_candidate()["answer"],
                                "node_ids": ids,
                            }
                        )
                    },
                )()

            async def aclose(self):
                pass

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        patched = candidate_bundle(
            base_bundle(), {"c_answer_shape": FIXED_C, "f_flight_pair": FIXED_F}, holder.name
        )
        spec = TaskSpec.load(TASK_YAML, patched)
        config = RunConfig(protocol_attempts=2, answer_attempts=2)
        runtime = KernelRuntime(patched, config)
        agent = AnswerAgent(runtime, StubClient(), config, spec, "paramretry")
        result = asyncio.run(agent.answer(question, graph))
        self.assertEqual(result.status, "answered", str(result.error))
        self.assertTrue(seen_feedback, "必须发生过一次契约拒绝反馈")
        self.assertTrue(result.evidence)
