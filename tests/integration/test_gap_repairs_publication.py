"""Offline regression scenarios for publication."""

import asyncio
import json
import tempfile
import unittest

from networkx import freeze

from darwinagent.agents.answer import AnswerAgent
from darwinagent.config import RunConfig
from darwinagent.contracts import GraphResult
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


class PublishRejectEntersFeedback(unittest.TestCase):
    """⑤⑥：引用无出处事实行 → 发布点契约错误 → 反馈（逐节点来源数）→ 换行重答成功。"""

    def test_unsourced_citation_retries_then_publishes(self):
        case = travel_case()
        question = case.questions[0]
        g = load_graph(FIXTURES / "graph.json")
        # 从工具真实可见的 Rockford 餐厅行里选节点（候选校验要求 node_ids ⊆ visible）
        from darwinagent.operators.data import DataCapabilities

        probe = GraphResult(freeze(g), {b.source.id: b for b in case.corpus})
        caps = DataCapabilities(probe)
        visible_rests = [
            rid
            for rid, r in caps.rows.items()
            if r.get("entity_type") == "restaurant" and "Rockford" in str(r.get("city", ""))
        ][:2]
        unsourced, healthy = visible_rests
        # 行 id(n…)->nx 节点键映射，无出处清的是底层节点的 __sources__
        g.nodes[caps.actual_ids[unsourced]]["__sources__"] = []
        graph = GraphResult(freeze(g), {b.source.id: b for b in case.corpus})

        seen_feedback = []

        class StubClient:
            def __init__(self):
                self.tool_turn = 0
                self.answer_turn = 0

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
                                    "limit": 50,
                                },
                            }
                        )
                    else:
                        content = json.dumps({"action": "ready"})
                    return type("Reply", (), {"content": content})()
                payload = json.loads(messages[-1]["content"])
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
                self.answer_turn += 1
                if self.answer_turn == 1:
                    ids = [unsourced]
                else:
                    ids = [healthy]
                    seen_feedback.append(payload.get("feedback"))
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

        spec = TaskSpec.load(TASK_YAML, base_bundle())
        config = RunConfig(protocol_attempts=2, answer_attempts=3)
        # base_bundle 的旧版 C 会误杀归档合法候选（T2 归档故障）——换修复版 C/F 的
        # 补丁 bundle，本测试聚焦发布点反馈环本身。
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        patched = candidate_bundle(
            base_bundle(), {"c_answer_shape": FIXED_C, "f_flight_pair": FIXED_F}, holder.name
        )
        spec = TaskSpec.load(TASK_YAML, patched)
        runtime = KernelRuntime(patched, config)
        agent = AnswerAgent(runtime, StubClient(), config, spec, "pubretry")
        result = asyncio.run(agent.answer(question, graph))
        self.assertEqual(result.status, "answered", str(result.error))
        self.assertTrue(result.evidence, "第二次候选必须带证据发布")
        # 第一次的发布拒绝以反馈形式进入第二次请求，且逐节点来源数明示 0
        self.assertTrue(seen_feedback and seen_feedback[0], "重答请求必须携带发布拒绝反馈")
        pub = [f for f in seen_feedback[0] if "publish_reject" in f]
        self.assertTrue(pub, seen_feedback[0])
        counts = pub[0]["publish_reject"]["cited_node_source_counts"]
        self.assertEqual(counts.get(unsourced), 0)
        self.assertEqual(agent.namespace, "pubretry")
