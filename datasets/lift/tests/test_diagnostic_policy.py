"""Audited worker16 and declared generation16384, using settled synthetic histories."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from darwinagent.agent import AgentTask, ExecutionBackend
from darwinagent.config import Config
from darwinagent.llm.client import BudgetExceeded, LLMClient
from darwinagent.runtime.artifacts import atomic_json, digest
from datasets.lift.tests.fixtures import worker16_fixture
from datasets.lift.transport import SharedTransport


class FixtureClient:
    def __init__(self, root):
        self.cfg = Config(
            model_strong="recorded",
            model_middle="recorded",
            model_fast="recorded",
            max_http_requests=100,
            model_profiles=False,
            work_dir=root,
        )
        self.requests = []

    def http_attempts(self):
        path = self.cfg.request_budget_path
        return json.loads(Path(path).read_text())["attempts"] if path and Path(path).exists() else 0

    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        atomic_json(self.cfg.request_budget_path, {"attempts": self.http_attempts() + 1})
        return SimpleNamespace(
            content=json.dumps({"final_response": "done", "artifacts": []}),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class WorkerPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_preserves136_history_and_exact16_boundary_new_roots(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            client = FixtureClient(root / "one")
            shared = worker16_fixture(client, root / "shared", attempts=136, amend_worker=False)
            protected = [Path(client.cfg.request_budget_path), *shared.root.rglob("*.json")]
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected}
            audit = shared.amend_worker_limit(worker_limit=16, expected_http_attempts=136)
            self.assertEqual(audit["http_attempts"], 136)
            returned = shared.amend_worker_limit(worker_limit=16, expected_http_attempts=136)
            self.assertEqual(returned, audit)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected})
            self.assertEqual(client.requests, [])
            audit_path = shared.root / "worker-limit-amendment.json"
            protected.append(audit_path)
            before[audit_path] = (audit_path.read_bytes(), audit_path.stat().st_mtime_ns)
            with self.assertRaises(BudgetExceeded):
                shared.check()
            with self.assertRaises(ValueError):
                SharedTransport(client, root / "shared", worker_limit=8)
            shared = SharedTransport(client, root / "shared", worker_limit=16)
            with self.assertRaisesRegex(BudgetExceeded, "Historical"):
                await shared.scoped("historical-worker", "worker").chat()
            for _ in range(16):
                await shared.scoped("new-worker", "worker").chat()
            with self.assertRaises(BudgetExceeded):
                await shared.scoped("new-worker", "worker").chat()
            self.assertEqual(len(client.requests), 16)
            client.cfg.work_dir = root / "two"
            fresh = SharedTransport(client, root / "shared", worker_limit=16)
            await fresh.scoped("fresh-worker", "worker").chat()
            counts = json.loads((shared.root / "worker-calls.json").read_text())
            self.assertEqual(counts, {"historical-worker": 8, "new-worker": 16, "fresh-worker": 1})
            for p in protected:
                if p not in {
                    Path(client.cfg.request_budget_path),
                    shared.root / "worker-calls.json",
                }:
                    self.assertEqual(before[p], (p.read_bytes(), p.stat().st_mtime_ns))
            counts["historical-worker"] = 9
            atomic_json(shared.root / "worker-calls.json", counts)
            with self.assertRaisesRegex(ValueError, "Historical"):
                SharedTransport(client, root / "shared", worker_limit=16)

    async def test_unsettled_or_global_gate_and_unamended16_are_refused(self):
        for fault in ("unknown", "submitted", "active", "trace", "attempts", "global"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                client = FixtureClient(root / "client")
                shared = worker16_fixture(client, root / "shared", attempts=136)
                # Test API gates before its worker audit exists, retaining original HTTP audit.
                (shared.root / "worker-limit-amendment.json").unlink()
                shared = SharedTransport(client, root / "shared", worker_limit=8)
                if fault in {"unknown", "submitted"}:
                    atomic_json(shared.root / "requests/unsettled.json", {"status": fault})
                elif fault == "active":
                    atomic_json(shared.root / "active-pause.json", {"status": "unknown"})
                elif fault == "trace":
                    atomic_json(shared.root / "trace-errors/one.json", {"failed": True})
                else:
                    atomic_json(
                        client.cfg.request_budget_path,
                        {"attempts": 137 if fault == "attempts" else 500},
                    )
                before = {
                    p: (p.read_bytes(), p.stat().st_mtime_ns) for p in shared.root.rglob("*.json")
                }
                with self.assertRaises(ValueError):
                    shared.amend_worker_limit(worker_limit=16, expected_http_attempts=136)
                self.assertEqual(
                    before,
                    {
                        p: (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in shared.root.rglob("*.json")
                    },
                )
                self.assertEqual(client.requests, [])
                with self.assertRaises(ValueError):
                    SharedTransport(client, root / "shared", worker_limit=16)

    async def test_all_roles_forward_declared_generation_and_effective_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            client = FixtureClient(root / "client")
            shared = worker16_fixture(client, root / "shared", attempts=136)
            shared.generation_max_tokens = 16384
            for role in ("worker", "judge", "wiki", "proposal", "schema", "admission"):
                await shared.scoped("new-" + role, role).chat(
                    role=role,
                    messages=[{"role": "user", "content": "fixture"}],
                    max_tokens=14000 if role == "proposal" else 4096,
                )
            self.assertEqual({r["max_tokens"] for r in client.requests}, {16384})
            self.assertEqual({r["temperature"] for r in client.requests}, {0})
            records = [
                json.loads(p.read_text())
                for p in (shared.root / "requests").glob("*.json")
                if not p.name.startswith("fixture-")
            ]
            self.assertEqual({r["max_tokens"] for r in records}, {16384})
            self.assertEqual(
                {r["request_fingerprint"] for r in records}, {digest(r) for r in client.requests}
            )

    async def test_reasoning_empty4096_then16384_wire_body_is_mechanical_not_json_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = Config(
                api_base_url="https://offline.invalid/v1",
                api_key="dummy",
                model_strong="glm-5.3-flash",
                max_http_requests=2,
                max_retries=1,
                model_profiles=False,
                work_dir=root,
            )
            cfg.role_tiers.update(worker="strong", judge="strong")
            bodies = []

            def handler(request):
                body = json.loads(request.content)
                bodies.append(body)
                small = body["max_tokens"] == 4096
                delta = (
                    {"reasoning_content": "r" * 7643}
                    if small
                    else {"content": '{"final_response":"done"}'}
                )
                chunk = {
                    "id": "fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "glm-5.3-flash",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 14008,
                        "completion_tokens": 4096 if small else 8192,
                        "total_tokens": 18104 if small else 22200,
                    },
                }
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                )

            async with LLMClient(cfg) as client:
                site = client._site_for(cfg.model_strong)[0]
                await site._client.aclose()
                site._client = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    event_hooks={"request": [client._reserve_http_attempt]},
                )
                first = await client.chat(
                    role="worker",
                    messages=[],
                    temperature=0,
                    max_tokens=4096,
                    durable=True,
                    use_cache=False,
                )
                second = await client.chat(
                    role="worker",
                    messages=[],
                    temperature=0,
                    max_tokens=16384,
                    durable=True,
                    use_cache=False,
                )
                self.assertEqual(first.content, "")
                self.assertEqual(first.usage["reasoning_chars"], 7643)
                self.assertTrue(first.usage["empty_content"])
                self.assertEqual(json.loads(second.content)["final_response"], "done")
            self.assertEqual([b["max_tokens"] for b in bodies], [4096, 16384])
            self.assertEqual([b["model"] for b in bodies], ["glm-5.3-flash"] * 2)
            self.assertEqual([b["temperature"] for b in bodies], [0] * 2)
            self.assertNotIn("thinking", bodies[1])
            self.assertNotEqual(
                LLMClient._cache_key("m", [], 0, 4096, True),
                LLMClient._cache_key("m", [], 0, 16384, True),
            )


class NativeDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_rounds4_plus3_plus2_complete_and_per_turn8_still_blocks9(self):
        from src.models import ExpectedResult, SuiteTask, TaskRequirements

        from datasets.lift.agents import NativeReAct, worker_profile
        from datasets.lift.kernel import initial_kernel
        from datasets.lift.local import LocalRunner
        from datasets.lift.local_tools import LocalTools
        from datasets.lift.tests.offline_local import infrastructure_guard

        class Dataset:
            def __init__(self):
                self.native = {
                    "q1": SuiteTask(
                        name="q1",
                        query="Deliver a fixture artifact",
                        category_name="fixture",
                        requirements=TaskRequirements(),
                        expected_result=ExpectedResult(
                            content_reqs="PRIVATE_CRITERION", trajectory_reqs=""
                        ),
                    )
                }

            def identity(self):
                return {"fixture": "native-three-round-correction", "q6": "sealed"}

            def task(self, task_id):
                return AgentTask(task_id, self.native[task_id].query)

            def stage(self, task, workspace):
                Path(workspace).mkdir(parents=True, exist_ok=True)

        class Client(FixtureClient):
            loop_forever = False

            async def chat(self, **kwargs):
                self.requests.append(kwargs)
                assert kwargs["max_tokens"] == 16384 and kwargs["temperature"] == 0
                if self.loop_forever:
                    answer = {"tool": "list_files", "params": {}}
                else:
                    request = json.loads(kwargs["messages"][1]["content"])
                    native = request["task"]["parameters"]["native_messages"]
                    round_no = sum(m["role"] == "assistant" for m in native)
                    step = sum(m["role"] == "assistant" for m in kwargs["messages"])
                    if kwargs["role"] == "judge":
                        assert "PRIVATE_CRITERION" in json.dumps(kwargs["messages"])
                        answer = {
                            "final_response": json.dumps(
                                {
                                    "success": round_no == 2,
                                    "score": 1 if round_no == 2 else 0.5,
                                    "reason": "Please correct the fixture artifact",
                                }
                            )
                        }
                    else:
                        assert "PRIVATE_CRITERION" not in json.dumps(kwargs["messages"])
                        count = (4, 3, 2)[round_no]
                        answer = (
                            {"final_response": "done", "artifacts": [{"path": "report.txt"}]}
                            if step == count - 1
                            else {
                                "tool": "write_text",
                                "params": {
                                    "path": "report.txt",
                                    "content": "third-round delivered",
                                },
                            }
                            if round_no == 2
                            else {"tool": "list_files", "params": {}}
                        )
                atomic_json(self.cfg.request_budget_path, {"attempts": self.http_attempts() + 1})
                return SimpleNamespace(
                    content=json.dumps(answer), usage={"prompt_tokens": 1, "completion_tokens": 1}
                )

        with tempfile.TemporaryDirectory() as d, infrastructure_guard():
            root = Path(d)
            client = Client(root / "client")
            shared = worker16_fixture(client, root / "budget/shared", attempts=136)
            runner = LocalRunner(
                dataset=Dataset(),
                shared=shared,
                root=root / "run",
                initial=initial_kernel(root / "run/K0"),
                run_id="native9",
                max_tokens=16384,
            )
            result = await runner.execute(
                runner.dataset.task("q1"), runner.initial, "campaign", "train", "fixture-q1"
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.usage["model_calls"], 9)
            self.assertEqual(result.usage["leaf_calls"], 6)
            worker_chat = next((runner.root / "sessions").glob("worker-*/chat.json"))
            turns = json.loads(worker_chat.read_text())["turns"]
            self.assertEqual([r["episode"]["usage"]["model_calls"] for r in turns], [4, 3, 2])
            files = {
                p: (p.read_bytes(), p.stat().st_mtime_ns)
                for base in ("sessions", "task-receipts", "workspaces", "loads")
                for p in (runner.root / base).rglob("*")
                if p.is_file()
            }
            requests = len(client.requests)
            await runner.execute(
                runner.dataset.task("q1"), runner.initial, "campaign", "train", "fixture-q1"
            )
            self.assertEqual(len(client.requests), requests)
            self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
            client.loop_forever = True
            backend = ExecutionBackend(
                NativeReAct(worker_session_cap=16, max_tokens=16384),
                LocalTools(root / "eight-turn").specs(),
                worker_profile(),
                shared.scoped("per-turn-eight", "worker"),
                root / "eight-receipts",
            )
            bounded = await backend.run(AgentTask("q", "deliver"), run_id="one")
            self.assertEqual(bounded.status, "budget_exhausted")
            self.assertEqual(bounded.usage["model_calls"], 8)
            self.assertEqual(len(client.requests) - requests, 8)
            self.assertEqual(backend.adapter.identity()["worker_session_cap"], 16)
            self.assertEqual(backend.adapter.identity()["worker_turn_model_cap"], 8)
            self.assertEqual(backend.adapter.identity()["max_tokens"], 16384)
