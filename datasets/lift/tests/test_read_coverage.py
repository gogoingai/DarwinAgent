"""Large-deliverable and current-version judgment regression contracts."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.runtime.artifacts import digest
from datasets.lift.full_tools import FullLocalTools


class ReadCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.output = self.workspace / "result/report.html"
        self.output.parent.mkdir(parents=True)
        self.text = "汉字🙂\n" * 21000
        self.output.write_text(self.text)
        self.tools = FullLocalTools(self.workspace, readonly=True)
        self.tools.audit_path = None
        self.steps = self.root / "judge/episodes/turn-0/steps"
        self.steps.mkdir(parents=True)
        self.index = 0

    async def page(self, **params):
        args = {"path": "result/report.html", **params}
        result = await self.tools.read_text(args)
        response = {"output": result}
        row = {
            "kind": "tool",
            "state": "responded",
            "input": {"name": "read_text", "params": args, "effect": "read"},
            "response": response,
            "response_digest": digest(response),
        }
        (self.steps / f"{self.index:06d}.json").write_text(json.dumps(row))
        self.index += 1
        return result

    def gate(self):
        from datasets.lift.read_coverage import ReadCoverageCheck

        return ReadCoverageCheck(self.workspace, self.root / "judge/episodes")

    def candidate(self, success=True):
        return {
            "status": "completed",
            "final_response": json.dumps(
                {"success": success, "reason": "", "score": 1 if success else 0}
            ),
            "artifacts": [],
        }

    async def read_all(self):
        first = await self.page()
        pieces = [first["content"]]
        current = first
        while current.get("next_offset") is not None:
            current = await self.page(
                offset=current["next_offset"], expected_sha256=first["sha256"]
            )
            pieces.append(current["content"])
        self.assertEqual("".join(pieces), self.text)
        return first

    async def test_large_utf8_deliverable_is_bounded_and_reconstructible(self):
        first = await self.read_all()
        self.assertEqual(first["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        for p in self.steps.glob("*.json"):
            r = json.loads(p.read_text())["response"]["output"]
            self.assertLess(len(json.dumps(r, ensure_ascii=False).encode()), 180000)

    async def test_control_characters_cannot_exceed_serialized_limit(self):
        self.text = "\x00" * 200000
        self.output.write_text(self.text)
        await self.read_all()

    async def test_page_version_mismatch_is_actionable(self):
        first = await self.page()
        self.output.write_text(self.text + "new")
        with self.assertRaisesRegex(Exception, "read_stale_file"):
            await self.page(offset=1, expected_sha256=first["sha256"])

    async def test_public_schema_exposes_pagination_and_version_binding(self):
        tool = next(t for t in self.tools.specs() if t.name == "read_text")
        self.assertTrue(
            {"offset", "limit", "expected_sha256"} <= set(tool.input_contract["properties"])
        )

    async def test_judge_success_without_reads_is_rejected(self):
        self.assertFalse(self.gate().check("final_delivery", self.candidate()))

    async def test_first_page_is_not_complete_evidence(self):
        await self.page()
        self.assertFalse(self.gate().check("final_delivery", self.candidate()))

    async def test_all_pages_allow_success_and_check_is_read_only(self):
        await self.read_all()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(self.gate().check("final_delivery", self.candidate()))
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )

    async def test_old_file_version_cannot_authorize_new_success(self):
        await self.read_all()
        self.output.write_text(self.text + "changed")
        self.assertFalse(self.gate().check("final_delivery", self.candidate()))

    async def test_tampered_read_receipt_is_rejected(self):
        await self.read_all()
        p = self.steps / "000000.json"
        row = json.loads(p.read_text())
        row["response"]["output"]["content"] = "forged"
        p.write_text(json.dumps(row))
        self.assertFalse(self.gate().check("final_delivery", self.candidate()))

    async def test_nonfinal_stages_and_failed_judgments_are_not_false_success(self):
        self.assertTrue(self.gate().check("before_action", {}))
        self.assertTrue(self.gate().check("final_delivery", self.candidate(False)))

    def make_judge(self, replies):
        from types import SimpleNamespace

        from darwinagent.agent import AgentTask
        from datasets.lift.full_agents import FullDarwinChatAgent

        client = SimpleNamespace()
        client.requests = []

        async def chat(**kwargs):
            client.requests.append(kwargs)
            index = len(client.requests) - 1
            response = (
                replies(kwargs, index)
                if callable(replies)
                else replies[min(index, len(replies) - 1)]
            )
            return SimpleNamespace(
                content=json.dumps(response),
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                elapsed_s=0.0,
            )

        client.chat = chat
        shared = SimpleNamespace(worker_limit=None, scoped=lambda *a: client)
        task = AgentTask("read-check", "Grade the saved delivery")
        agent = FullDarwinChatAgent(
            task,
            self.tools.specs(),
            shared,
            self.root / "judge",
            None,
            role="judge",
            run_id="offline",
            session="judge-offline",
        )
        return agent, task, client

    async def test_native_judge_wires_gate_and_recovers_by_reading_all_pages(self):
        final = {"final_response": self.candidate()["final_response"]}

        def replies(request, index):
            if index == 0:
                return final
            if index == 1:
                return {"tool": "read_text", "params": {"path": "result/report.html"}}
            observation = json.loads(request["messages"][-1]["content"])["observation"]
            if observation.get("next_offset") is not None:
                return {
                    "tool": "read_text",
                    "params": {
                        "path": "result/report.html",
                        "offset": observation["next_offset"],
                        "expected_sha256": observation["sha256"],
                    },
                }
            return final

        agent, task, client = self.make_judge(replies)
        self.assertTrue(agent.backend.profile.host_checks)
        result = await agent.backend.run(task, run_id="turn-0")
        self.assertEqual(result.status, "completed")
        self.assertGreater(result.usage["leaf_calls"], 1)
        checks = [
            e for e in result.events if e["kind"] == "check" and e.get("stage") == "final_delivery"
        ]
        self.assertTrue(any(not o["accepted"] for e in checks for o in e["outcomes"]))
        self.assertTrue(all(o["accepted"] for o in checks[-1]["outcomes"]))
        self.assertEqual(len(client.requests), result.usage["leaf_calls"] + 2)

    async def test_native_judge_repeated_unread_success_is_bounded(self):
        final = {"final_response": self.candidate()["final_response"]}
        agent, task, client = self.make_judge([final])
        self.assertEqual(agent.backend.adapter.max_delivery_rejections, 8)
        result = await agent.backend.run(task, run_id="turn-0")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(client.requests), 8)
