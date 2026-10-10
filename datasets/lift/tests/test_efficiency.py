"""Offline efficiency, graph material loading and immutable total-cap amendment."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
import yaml

from darwinagent.agent import AgentKernelAssets, AgentTask, ExecutionBackend, ReActAgent
from darwinagent.config import Config
from darwinagent.llm.client import BudgetExceeded, LLMClient
from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.data import sha
from datasets.lift.kernel import initial_kernel
from datasets.lift.local_tools import LocalTools
from datasets.lift.transport import SharedTransport
from tests.support.agent_runtime import RecordedClient


class MaterialLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_dependent_loader_counts_real_reads_with_fixed_prompt_and_checks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = root / "files"
            files.mkdir()
            (files / "rules.txt").write_text("public rules")
            (files / "table.csv").write_text("amount\n3\n")
            (files / "alternate.txt").write_text("different public graph input")
            k0 = initial_kernel(root / "K0")
            schema = yaml.safe_load(k0.get("schema").content)
            schema["nodes"][1]["fields"]["path"]["path"] = "alternate"
            changed = AgentKernelAssets(
                tuple(
                    replace(a, content=yaml.safe_dump(schema)) if a.kind == "S" else a
                    for a in k0.assets.assets
                ),
                k0.assets.profile,
            ).export(root / "K1")
            self.assertEqual(k0.get("worker").content, changed.get("worker").content)
            task = AgentTask(
                "q",
                "read public materials",
                (
                    {"path": "rules.txt", "alternate": "alternate.txt"},
                    {"path": "table.csv", "alternate": "alternate.txt"},
                ),
            )
            outputs = []
            for index, kernel in enumerate((k0, changed)):
                client = RecordedClient(
                    [
                        {"tool": "inspect_materials", "params": {}},
                        {"final_response": ""},
                        {"final_response": "done"},
                    ]
                )
                backend = ExecutionBackend(
                    ReActAgent(),
                    LocalTools(files).specs(),
                    k0.assets.profile,
                    client,
                    root / "receipts",
                )
                result = await backend.run(task, kernel, run_id=str(index))
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.usage["model_calls"], 3)
                self.assertEqual(result.usage["leaf_calls"], 2)
                self.assertEqual(sum(e["kind"] == "macro" for e in result.events), 1)
                self.assertTrue(
                    any(
                        e["kind"] == "check"
                        and e["stage"] == "final_delivery"
                        and not e["outcomes"][0]["accepted"]
                        for e in result.events
                    )
                )
                outputs.append(next(e["output"] for e in result.events if e["kind"] == "macro"))
            self.assertEqual([r["path"] for r in outputs[0]["loaded"]], ["rules.txt", "table.csv"])
            self.assertEqual([r["path"] for r in outputs[1]["loaded"]], ["alternate.txt"] * 2)
            self.assertNotEqual(outputs[0], outputs[1])


class CapAmendmentTests(unittest.IsolatedAsyncioTestCase):
    def cfg(self, root, cap=100, work="one"):
        return Config(
            api_key="offline-dummy",
            api_base_url="https://offline.invalid/v1",
            model_strong="offline",
            max_http_requests=cap,
            max_retries=1,
            model_profiles=False,
            request_budget_path=root / "http-attempts.json",
            work_dir=root / work,
        )

    def fixture(self, root, client):
        shared = SharedTransport(client, root / "shared")
        atomic_json(root / "http-attempts.json", {"attempts": 78})
        atomic_json(shared.root / "worker-calls.json", {"historical-worker": 8})
        for n in range(78):
            atomic_json(
                shared.root / "requests" / f"old-{n:03}.json",
                {"status": "completed", "actual_http_attempts": 1},
            )
        return shared

    async def test_explicit_amendment_preserves_history_and_501_production_hook_blocks_across_roots(
        self,
    ):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            async with LLMClient(self.cfg(root)) as old:
                shared = self.fixture(root, old)
                atomic_json(shared.root / "pause.json", {"status": "budget_exhausted"})
                atomic_json(
                    shared.root / "recovery/local-cap.json",
                    {"pause_sha256": sha(shared.root / "pause.json")},
                )
                protected = [root / "http-attempts.json", *shared.root.rglob("*.json")]
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected}
                audit = shared.amend_http_cap(total_cap=500, expected_http_attempts=78)
                self.assertEqual(len(audit["settled_receipts"]), 78)
                self.assertEqual(
                    before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected}
                )
                with self.assertRaises(BudgetExceeded):
                    shared.check()
                with self.assertRaises(ValueError):
                    SharedTransport(old, shared.root)
            atomic_json(root / "http-attempts.json", {"attempts": 499})
            requests = []
            for work in ("new-a", "new-b"):
                async with LLMClient(self.cfg(root, 500, work)) as client:
                    current = SharedTransport(client, root / "shared")
                    self.assertEqual(current.identity()["http_cap"], 500)
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(
                            lambda request: requests.append(request) or httpx.Response(200)
                        ),
                        event_hooks={"request": [client._reserve_http_attempt]},
                    ) as mock:
                        if work == "new-a":
                            await mock.get("https://offline.invalid")
                        else:
                            with self.assertRaises(BudgetExceeded):
                                await mock.get("https://offline.invalid")
                            with self.assertRaises(BudgetExceeded):
                                current.check()
                    self.assertEqual(client.http_attempts(), 500)
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                json.loads((root / "shared/active-pause.json").read_text())["status"],
                "budget_exhausted",
            )
            self.assertEqual(json.loads((root / "shared/policy.json").read_text())["http_cap"], 100)
            for p in protected:
                if p != root / "http-attempts.json":
                    self.assertEqual(before[p], (p.read_bytes(), p.stat().st_mtime_ns))
            (root / "shared/requests/old-000.json").write_text("{}")
            async with LLMClient(self.cfg(root, 500, "tamper")) as client:
                with self.assertRaisesRegex(ValueError, "settled"):
                    SharedTransport(client, root / "shared")

    async def test_unknown_global_pause_or_mismatched_ledger_refuse_amendment(self):
        for fault in ("unknown", "submitted", "pause", "attempts", "trace"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                async with LLMClient(self.cfg(root)) as client:
                    shared = self.fixture(root, client)
                    if fault in {"unknown", "submitted"}:
                        atomic_json(shared.root / "requests/lost.json", {"status": fault})
                    elif fault == "pause":
                        atomic_json(
                            shared.root / "pause.json",
                            {"status": "budget_exhausted", "gate_id": "global"},
                        )
                    elif fault == "attempts":
                        atomic_json(root / "http-attempts.json", {"attempts": 100})
                    else:
                        atomic_json(shared.root / "trace-errors/one.json", {"failed": True})
                    with self.assertRaises(ValueError):
                        shared.amend_http_cap(total_cap=500, expected_http_attempts=78)
                    self.assertFalse((shared.root / "http-cap-amendment.json").exists())
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            async with LLMClient(self.cfg(root, 500)) as client:
                with self.assertRaisesRegex(ValueError, "explicit"):
                    SharedTransport(client, root / "shared")
