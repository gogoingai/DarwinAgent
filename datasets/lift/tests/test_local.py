"""Local fixed-tool boundaries and native failure scheduling without infrastructure."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from darwinagent.agent import AgentTask
from darwinagent.config import Config
from darwinagent.llm.client import BudgetExceeded
from datasets.lift import toolbox
from datasets.lift.local_tools import LocalTools
from datasets.lift.tools import NAMES, DockerTools
from datasets.lift.transport import SharedTransport

try:
    import src.lift  # noqa: F401
except ModuleNotFoundError:
    NATIVE = False
else:
    NATIVE = True


class LocalToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_declarations_outputs_context_isolation_and_readonly(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first, second = LocalTools(root / "one"), LocalTools(root / "two")
            self.assertEqual(
                [t.declaration() for t in first.specs()],
                [t.declaration() for t in DockerTools("unused", root / "one").specs()],
            )
            contents = ["name,amount\na,2\na,3\n", "name,amount\nb,9\n"]
            writes = await asyncio.gather(
                *(
                    tools.write_text({"path": "table.csv", "content": text})
                    for tools, text in zip((first, second), contents)
                )
            )
            self.assertEqual([w["bytes"] for w in writes], [len(c) for c in contents])
            self.assertIsNot(first.toolbox, second.toolbox)
            self.assertEqual(toolbox.ROOT, Path("/workspace/task"))
            for tools, contents in zip((first, second), contents):
                with patch.object(toolbox, "ROOT", Path(tools.workspace)):
                    self.assertEqual(
                        await tools.read_table({"path": "table.csv"}),
                        toolbox.execute("read_table", {"path": "table.csv"}),
                    )
                self.assertEqual(
                    (await tools.read_text({"path": "table.csv"}))["content"], contents
                )
            readonly = LocalTools(root / "one", readonly=True)
            self.assertEqual(tuple(t.name for t in readonly.specs()), NAMES[:-1])
            with self.assertRaises(PermissionError):
                await readonly.invoke("write_text", {"path": "judge.txt", "content": "denied"})
            self.assertFalse((root / "one/judge.txt").exists())
            self.assertNotIn("os", readonly.toolbox.__dict__)
            self.assertNotIn("subprocess", readonly.toolbox.__dict__)

    async def test_paths_unknown_sql_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "workspace"
            root.mkdir()
            tools = LocalTools(root)
            await tools.write_text({"path": "data.csv", "content": "x\n1\n"})
            for path in ("../judge.json", str(Path(d) / "secret.env")):
                with self.assertRaises(ValueError):
                    await tools.read_text({"path": path})
            with self.assertRaises(ValueError):
                await tools.invoke("shell", {"command": "pwd"})
            with self.assertRaises(Exception):
                await tools.transform_tables({"tables": {"t": "data.csv"}, "sql": "DELETE FROM t"})
            (root / "link").symlink_to(root / "data.csv")
            with self.assertRaises(ValueError):
                await tools.read_text({"path": "link"})


class TinyDataset:
    def __init__(self):
        from src.models import ExpectedResult, SuiteTask, TaskRequirements

        self.native = {
            name: SuiteTask(
                name=name,
                query="Deliver " + name,
                category_name="local",
                requirements=TaskRequirements(),
                expected_result=ExpectedResult(
                    content_reqs="PRIVATE_CRITERION", trajectory_reqs=""
                ),
            )
            for name in ("q1", "q2", "q5")
        }

    def identity(self):
        return {"fixed": "tiny-recorded", "q6": "sealed_not_opened"}

    def task(self, name):
        return AgentTask(name, self.native[name].query)

    def stage(self, task, workspace):
        Path(workspace).mkdir(parents=True, exist_ok=True)


class LocalClient:
    def __init__(self, root, fault=None):
        self.cfg = Config(
            model_strong="recorded",
            model_middle="recorded",
            model_fast="recorded",
            max_http_requests=100,
            max_retries=1,
            model_profiles=False,
            work_dir=root,
        )
        self.calls, self.fault = [], fault

    def http_attempts(self):
        return 0

    async def chat(self, **kwargs):
        role = kwargs["role"]
        if role not in {"worker", "judge"}:
            raise AssertionError("Unhealthy training must not dispatch proposal or Wiki")
        request = json.loads(kwargs["messages"][1]["content"])
        task = request["task"]["id"]
        tools = request["tools"]
        self.calls.append((task, role))
        if role == "worker":
            assert "PRIVATE_CRITERION" not in json.dumps(kwargs["messages"])
            fail = task == "q1" or (
                task == "q5" and all(t["name"] != "inspect_materials" for t in tools)
            )
            if self.fault == "unknown":
                raise RuntimeError("Uncertain response")
            if self.fault == "known_http" and task == "q1":
                import httpx
                from openai import APIStatusError

                response = httpx.Response(
                    400, request=httpx.Request("POST", "https://example.invalid")
                )
                raise APIStatusError("settled provider failure", response=response, body=None)
            answer = (
                {"tool": "list_files", "params": {}}
                if fail
                else {"final_response": "done", "artifacts": []}
            )
        else:
            assert (
                "PRIVATE_CRITERION"
                in request["task"]["parameters"]["native_messages"][-1]["content"]
            )
            assert all(t["name"] != "write_text" for t in tools)
            answer = {
                "final_response": json.dumps({"success": True, "reason": "", "score": 1}),
                "artifacts": [],
            }
        return SimpleNamespace(
            content=json.dumps(answer), usage={"prompt_tokens": 1, "completion_tokens": 1}
        )


@unittest.skipUnless(NATIVE, "Pinned native Python environment required")
class LocalSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, root, fault=None):
        from datasets.lift.kernel import initial_kernel
        from datasets.lift.local import LocalRunner

        client = LocalClient(root, fault)
        shared = SharedTransport(client, root / "budget/shared")
        return LocalRunner(
            dataset=TinyDataset(),
            shared=shared,
            root=root / "run",
            initial=initial_kernel(root / "run/K0"),
            run_id="local-test",
        ), client

    async def test_known_local_caps_continue_q2_and_failed_a_does_not_skip_b_c(self):
        from datasets.lift.tests.offline_local import infrastructure_guard

        with tempfile.TemporaryDirectory() as d, infrastructure_guard():
            runner, client = self.runner(Path(d))
            result = await runner.run()
            final = {r["arm"]: r for r in result["rows"] if r["task_id"] == "q5"}
            self.assertEqual(
                {arm: r["status"] for arm, r in final.items()},
                {"A": "failed", "B": "completed", "C": "completed"},
            )
            self.assertIsNone(final["A"]["native"]["content_score"])
            self.assertTrue(result["selected"]["fallback"])
            self.assertFalse(result["acceptance_complete"])
            self.assertEqual(sum(t == "q1" and role == "worker" for t, role in client.calls), 8)
            self.assertIn(("q2", "judge"), client.calls)
            before = len(client.calls)
            await runner.run()
            self.assertEqual(len(client.calls), before)

    async def test_unknown_and_global_stop_before_next_task(self):
        from datasets.lift.tests.offline_local import infrastructure_guard

        for fault in ("unknown", "global"):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as d,
                infrastructure_guard(),
            ):
                runner, client = self.runner(Path(d), fault)
                if fault == "global":
                    client.http_attempts = lambda: 100
                with self.assertRaises(BudgetExceeded):
                    await runner.run()
                self.assertFalse(any(task != "q1" for task, _ in client.calls))
                result = json.loads((runner.root / "all-pair-report.json").read_text())
                self.assertEqual(result["status"], "paused")
                self.assertFalse(result["acceptance_complete"])
                self.assertFalse((runner.root / "selected-state.json").exists())

    async def test_known_provider_failure_is_settled_then_next_tasks_continue(self):
        from datasets.lift.tests.offline_local import infrastructure_guard

        with tempfile.TemporaryDirectory() as d, infrastructure_guard():
            runner, client = self.runner(Path(d), "known_http")
            result = await runner.run()
            self.assertIn(("q2", "judge"), client.calls)
            self.assertEqual(
                {r["arm"] for r in result["rows"] if r["task_id"] == "q5"}, {"A", "B", "C"}
            )
            self.assertFalse((runner.shared.root / "pause.json").exists())
            records = [
                json.loads(p.read_text()) for p in (runner.shared.root / "requests").glob("*.json")
            ]
            self.assertTrue(any(r["status"] == "failed" for r in records))
            self.assertFalse(any(r["status"] in {"unknown", "submitted"} for r in records))


class LocalBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_100_request_preflight_is_shared_across_independent_roots(self):
        import httpx

        from darwinagent.llm.client import LLMClient
        from darwinagent.runtime.artifacts import atomic_json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ledger = root / "http-attempts.json"
            atomic_json(ledger, {"attempts": 99})
            requests = []

            def handle(request):
                requests.append(request)
                return httpx.Response(200, json={"ok": True})

            clients = [
                LLMClient(
                    Config(
                        model_strong="recorded",
                        max_http_requests=100,
                        max_retries=1,
                        request_budget_path=ledger,
                        work_dir=root / campaign,
                    )
                )
                for campaign in ("one", "two")
            ]
            for client in clients:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handle),
                    trust_env=False,
                    event_hooks={"request": [client._reserve_http_attempt]},
                ) as http:
                    if not requests:
                        await http.post("https://mock.invalid/attempt")
                    else:
                        with self.assertRaises(BudgetExceeded) as denied:
                            await http.post("https://mock.invalid/attempt")
                        self.assertFalse(denied.exception.dispatched)
                shared = SharedTransport(client, root / "shared")
                with self.assertRaises(BudgetExceeded):
                    await shared.scoped("another-task", "worker").chat(role="worker", messages=[])
                await client.aclose()
            self.assertEqual(len(requests), 1)
            self.assertEqual(json.loads(ledger.read_text()), {"attempts": 100})
            self.assertFalse((root / "shared/worker-calls.json").exists())
