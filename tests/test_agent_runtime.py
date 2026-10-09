"""Generic runtime behavior: two loops, real artifacts, immutable receipts and v3 coexistence."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.agent import (
    AgentAsset,
    AgentTask,
    ExecutionBackend,
    ExecutionProfile,
    ReActAgent,
    file_tools,
)
from darwinagent.agent.graph import project, validate_schema
from darwinagent.agent.interpreter import AsyncInterpreter
from darwinagent.kernel import KernelBundle
from darwinagent.operators.sandbox import Limits, SandboxError, admit
from darwinagent.runtime.artifacts import digest
from tests.support.agent_runtime import (
    LostReceiptClient,
    ModelTwiceAgent,
    RecordedClient,
    ScriptedAgent,
    TwoWritesAgent,
    bundle,
    schema,
)


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = AgentTask(
            "task",
            "Write a report",
            ({"content": "first evidence", "corrected": "second evidence"},),
        )
        self.tools = file_tools(self.root / "files")
        self.profile = ExecutionProfile("fixed", tuple(t.name for t in self.tools))

    def backend(self, agent, *, client=None, profile=None, tools=None):
        return ExecutionBackend(
            agent, tools or self.tools, profile or self.profile, client, self.root / "runs"
        )

    async def test_s_and_f_change_real_artifacts_with_p_fixed(self):
        kernels = [
            bundle(self.root / "k0", self.profile),
            bundle(self.root / "k1", self.profile, source_path="corrected"),
            bundle(self.root / "k2", self.profile, uppercase=True),
        ]
        prompt_ids = [k.get("worker").fingerprint for k in kernels]
        self.assertEqual(len(set(prompt_ids)), 1)
        outputs = []
        for index, kernel in enumerate(kernels):
            result = await self.backend(ScriptedAgent()).run(self.task, kernel, run_id=f"s{index}")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.usage["leaf_calls"], 1)
            self.assertEqual(sum(e["kind"] == "tool" for e in result.events), 1)
            outputs.append((self.root / "files/report.txt").read_text())
        self.assertEqual(outputs, ["first evidence", "second evidence", "FIRST EVIDENCE"])
        loaded = KernelBundle(self.root / "k0")
        self.assertEqual(loaded.content_id, kernels[0].content_id)

    async def test_react_baseline_uses_identical_registered_tools_and_measured_tokens(self):
        client = RecordedClient(
            [
                {"tool": "write_file", "params": {"path": "react.txt", "content": "real artifact"}},
                {"final_response": "done", "artifacts": []},
            ]
        )
        result = await self.backend(ReActAgent(), client=client).run(self.task, run_id="react")
        self.assertEqual(result.status, "completed")
        self.assertEqual((self.root / "files/react.txt").read_text(), "real artifact")
        self.assertEqual(result.usage["prompt_tokens"], 20)
        self.assertEqual(result.usage["completion_tokens"], 6)
        self.assertEqual(result.usage["model_calls"], 2)
        self.assertEqual(result.usage["leaf_calls"], 1)
        saved = await self.backend(ReActAgent(), client=client).run(self.task, run_id="react")
        self.assertEqual(saved.to_dict(), result.to_dict())
        self.assertEqual(client.calls, 2)

    async def test_interrupt_restart_reuses_write_receipt(self):
        writes = []
        original = self.tools[1]

        def counted(params):
            writes.append(params["path"])
            return original.handler(params)

        tools = (self.tools[0], replace(original, handler=counted))
        agent = TwoWritesAgent()
        agent.interrupt = True
        with self.assertRaises(asyncio.CancelledError):
            await self.backend(agent, tools=tools).run(self.task, run_id="restart")
        self.assertEqual(writes, ["first.txt"])
        before = (self.root / "runs/restart/steps/000000.json").read_bytes()
        agent.interrupt = False
        result = await self.backend(agent, tools=tools).run(self.task, run_id="restart")
        self.assertEqual(result.status, "completed")
        self.assertEqual(writes, ["first.txt", "second.txt"])
        self.assertEqual(before, (self.root / "runs/restart/steps/000000.json").read_bytes())
        self.assertEqual(result.usage["leaf_calls"], 2)

    async def test_unknown_write_is_quarantined_and_never_reexecuted(self):
        writes = []

        def uncertain(params):
            writes.append(params["path"])
            (self.root / "files" / params["path"]).write_text(params["content"])
            raise ConnectionError("receipt lost")

        tools = (self.tools[0], replace(self.tools[1], handler=uncertain))
        backend = self.backend(TwoWritesAgent(), tools=tools)
        first = await backend.run(self.task, run_id="unknown")
        second = await backend.run(self.task, run_id="unknown")
        self.assertEqual((first.status, second.status), ("unknown", "unknown"))
        self.assertEqual(writes, ["first.txt"])
        self.assertEqual(second.usage["unknown_calls"], 1)
        self.assertFalse((self.root / "runs/unknown/result.json").exists())

    async def test_final_delivery_check_rejects_actual_output(self):
        check = AgentAsset(
            "reject",
            "C",
            'def check(candidate):\n    return candidate["final_response"] != "delivered"\n',
            stage="final_delivery",
            schema_dependencies=("schema",),
        )
        kernel = bundle(self.root / "reject", self.profile, checks=(check,))
        result = await self.backend(ScriptedAgent()).run(self.task, kernel, run_id="rejected")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.final_response, "")
        self.assertTrue((self.root / "files/report.txt").exists())
        final = next(
            e for e in result.events if e["kind"] == "check" and e["stage"] == "final_delivery"
        )
        self.assertFalse(final["outcomes"][0]["accepted"])

    async def test_before_action_check_prevents_write(self):
        check = AgentAsset(
            "deny",
            "C",
            'def check(candidate):\n    return candidate["effect"] != "write"\n',
            stage="before_action",
            schema_dependencies=("schema",),
        )
        kernel = bundle(self.root / "deny", self.profile, checks=(check,))
        result = await self.backend(ScriptedAgent()).run(self.task, kernel, run_id="denied")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.usage["leaf_calls"], 0)
        self.assertFalse((self.root / "files/report.txt").exists())

    async def test_after_action_check_rejects_receipted_write(self):
        check = AgentAsset(
            "after",
            "C",
            'def check(candidate):\n    return candidate["effect"] != "write"\n',
            stage="after_action",
            schema_dependencies=("schema",),
        )
        kernel = bundle(self.root / "after", self.profile, checks=(check,))
        result = await self.backend(ScriptedAgent()).run(self.task, kernel, run_id="after")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.usage["leaf_calls"], 1)
        self.assertEqual((self.root / "files/report.txt").read_text(), "first evidence")
        receipt = json.loads((self.root / "runs/after/steps/000000.json").read_text())
        self.assertEqual(receipt["state"], "responded")

    async def test_fixed_host_check_cannot_be_removed_by_kernel(self):
        def host_check(stage, candidate):
            return stage != "final_delivery"

        fixed = replace(self.profile, host_checks=(host_check,))
        kernel = bundle(self.root / "host", fixed)
        backend = self.backend(ScriptedAgent(), profile=fixed)
        self.assertTrue(backend.admit(kernel)["accepted"])
        self.assertEqual(kernel.assets.content_id, kernel.content_id)
        result = await backend.run(self.task, kernel, run_id="host")
        self.assertEqual(result.status, "rejected")
        with self.assertRaisesRegex(ValueError, "profile"):
            self.backend(ScriptedAgent()).admit(kernel)

    async def test_model_budget_is_durable_and_receipts_are_not_billed_twice(self):
        client = RecordedClient([{"tool": "read_file", "params": {"path": "source.txt"}}])
        (self.root / "files/source.txt").write_text("source")
        profile = replace(self.profile, max_model_calls=1)
        result = await self.backend(ReActAgent(), client=client, profile=profile).run(
            self.task, run_id="budget"
        )
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.usage["model_calls"], 1)
        self.assertEqual(client.calls, 1)
        result = await self.backend(ReActAgent(), client=client, profile=profile).run(
            self.task, run_id="budget"
        )
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.usage["model_calls"], 1)

    async def test_model_budget_survives_interruption_and_replay(self):
        client = RecordedClient([{"first": True}, {"second": True}])
        agent = ModelTwiceAgent()
        agent.interrupt = True
        backend = self.backend(
            agent, client=client, profile=replace(self.profile, max_model_calls=1)
        )
        with self.assertRaises(asyncio.CancelledError):
            await backend.run(self.task, run_id="model-restart")
        agent.interrupt = False
        result = await backend.run(self.task, run_id="model-restart")
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.usage["model_calls"], 1)

    async def test_unknown_model_receipt_is_not_resubmitted(self):
        client = LostReceiptClient()
        backend = self.backend(ModelTwiceAgent(), client=client)
        self.assertEqual((await backend.run(self.task, run_id="model-unknown")).status, "unknown")
        self.assertEqual((await backend.run(self.task, run_id="model-unknown")).status, "unknown")
        self.assertEqual(client.calls, 1)

    async def test_leaf_receipt_tampering_is_detected(self):
        agent = TwoWritesAgent()
        agent.interrupt = True
        backend = self.backend(agent)
        with self.assertRaises(asyncio.CancelledError):
            await backend.run(self.task, run_id="tamper")
        path = self.root / "runs/tamper/steps/000000.json"
        row = json.loads(path.read_text())
        row["response"]["output"]["path"] = "forged.txt"
        path.write_text(json.dumps(row))
        agent.interrupt = False
        result = await backend.run(self.task, run_id="tamper")
        self.assertEqual(result.status, "failed")
        self.assertFalse((self.root / "files/second.txt").exists())

    async def test_async_interpreter_awaits_and_enforces_bounds(self):
        calls = []

        async def call(name, params):
            calls.append(name)
            await asyncio.sleep(0)
            return params

        fn = admit(
            'def run(params):\n    out = []\n    for i in range(3):\n        out.append(call("read", {"i":i}))\n    return {"items":out}\n',
            capability_names={"call", "view"},
        )
        result = await AsyncInterpreter(fn, {"call": call}).execute({})
        self.assertEqual(result, {"items": [{"i": 0}, {"i": 1}, {"i": 2}]})
        self.assertEqual(calls, ["read"] * 3)
        with self.assertRaises(SandboxError):
            await AsyncInterpreter(fn, {"call": call}, Limits(steps=4)).execute({})

        async def slow(name, params):
            await asyncio.sleep(0.05)
            return params

        with self.assertRaisesRegex(SandboxError, "time budget"):
            await AsyncInterpreter(fn, {"call": slow}, Limits(timeout_s=0.005)).execute({})
        with self.assertRaises(SandboxError):
            admit(
                'def run(params):\n    return open("secret")\n', capability_names={"call", "view"}
            )

    def test_projection_has_typed_fields_and_source_bound_relations(self):
        declaration = schema()
        declaration["nodes"].append(
            {
                "id": "Event",
                "source": "events",
                "fields": {"text": {"path": "output.content", "type": "string"}},
                "where": {"kind": "tool"},
            }
        )
        declaration["relations"].append(
            {
                "id": "derived",
                "from": "Document",
                "to": "Event",
                "from_field": "text",
                "to_field": "text",
            }
        )
        events = [{"kind": "tool", "output": {"content": "first evidence"}}]
        graph = project(declaration, self.task, events)
        self.assertEqual(len(graph["relations"]), 1)
        self.assertEqual(
            graph["nodes"][0]["source_ref"]["digest"], digest(self.task.to_dict()["attachments"][0])
        )
        self.assertEqual(len(graph["relations"][0]["source_refs"]), 2)
        self.assertEqual(events, [{"kind": "tool", "output": {"content": "first evidence"}}])
        bad = schema("missing")
        with self.assertRaisesRegex(ValueError, "Unmaterializable"):
            project(bad, self.task, [])
        bad = schema()
        bad["nodes"][0]["fields"]["text"]["type"] = "magic"
        with self.assertRaises(ValueError):
            validate_schema(bad)

    async def test_profile_and_task_cannot_change_on_resume(self):
        kernel = bundle(self.root / "profile", self.profile)
        with self.assertRaisesRegex(ValueError, "profile"):
            self.backend(ScriptedAgent(), profile=replace(self.profile, max_model_calls=9)).admit(
                kernel
            )
        client = RecordedClient([{"final_response": "done"}])
        backend = self.backend(ReActAgent(), client=client)
        await backend.run(self.task, run_id="identity")
        with self.assertRaisesRegex(ValueError, "identity"):
            await backend.run(replace(self.task, instruction="Changed"), run_id="identity")
