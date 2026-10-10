"""One current view preserves observations, graph state and durable execution."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from darwinagent.agent import (
    AgentAsset,
    AgentKernelAssets,
    AgentTask,
    ExecutionBackend,
    ExecutionProfile,
    ReActAgent,
    file_tools,
)
from tests.support.agent_runtime import RawRecordedClient


class LayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_layout_is_bound_and_old_layout_cannot_reuse_completed_root(self):
        with tempfile.TemporaryDirectory() as d:
            client = RawRecordedClient([json.dumps({"final_response": "done"})])
            backend = ExecutionBackend(
                ReActAgent(), [], ExecutionProfile("layout", ()), client, Path(d)
            )
            task = AgentTask("q", "inspect")
            await backend.run(task, run_id="one")
            identity = backend.identity()
            self.assertEqual(backend._adapter_configuration()["view_layout"], "latest-view-v1")
            object.__setattr__(backend.adapter, "view_layout", "cumulative-v0")
            self.assertNotEqual(identity, backend.identity())
            with self.assertRaisesRegex(ValueError, "identity changed"):
                await backend.run(task, run_id="one")
            self.assertEqual(len(client.requests), 1)

    async def test_raw_history_once_observations_errors_checks_and_replay_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tools = file_tools(root / "files")
            (root / "files/first").write_text("FIRST DISTINCT RESULT")
            (root / "files/second").write_text("SECOND DISTINCT RESULT")
            task = AgentTask(
                "q1",
                "inspect",
                ({"content": "PUBLIC ATTACHMENT"},),
                {"native_messages": [{"role": "user", "content": "history " * 1400}]},
            )
            client = RawRecordedClient(
                [
                    json.dumps({"tool": "read_file", "params": {"path": "first"}}),
                    "not JSON",
                    json.dumps(
                        {
                            "actions": [
                                {"tool": "read_file", "params": {"path": "second"}},
                                {"tool": "read_file", "params": {"path": "missing"}},
                            ]
                        }
                    ),
                    json.dumps({"final_response": "done"}),
                ]
            )
            backend = ExecutionBackend(
                ReActAgent(),
                tools,
                ExecutionProfile("layout", tuple(t.name for t in tools)),
                client,
                root / "runs",
            )
            result = await backend.run(task, run_id="one")
            self.assertEqual(result.status, "completed")
            for request in client.requests:
                users = [
                    json.loads(m["content"]) for m in request["messages"] if m["role"] == "user"
                ]
                self.assertEqual(sum("view" in u for u in users), 1)
                self.assertEqual(sum("task" in u for u in users), 1)
                self.assertFalse("task" in users[-1]["view"])
                self.assertEqual(users[0]["task"], task.to_dict())
            users = [
                json.loads(m["content"])
                for m in client.requests[-1]["messages"]
                if m["role"] == "user"
            ]
            observations = [u["observation"] for u in users if "observation" in u]
            self.assertEqual(observations[0]["content"], "FIRST DISTINCT RESULT")
            self.assertEqual(observations[1]["error"]["error_type"], "JSONDecodeError")
            self.assertEqual(
                observations[2]["actions"][0]["observation"]["content"], "SECOND DISTINCT RESULT"
            )
            self.assertEqual(
                observations[2]["actions"][1]["observation"]["error"]["error_type"],
                "FileNotFoundError",
            )
            events = users[-1]["view"]["events"]
            self.assertEqual(events, list(result.to_dict()["events"][: len(events)]))
            self.assertTrue(any(e["kind"] == "check" for e in events))
            self.assertTrue(any(e["kind"] == "tool_failure" for e in events))
            # Size-only counterexample to the old q1 layout, never a token/quality claim.
            latest_chars = len(json.dumps(client.requests[-1]["messages"]))
            old = json.loads(json.dumps(client.requests[-1]["messages"]))
            for m in old:
                if m["role"] == "user":
                    u = json.loads(m["content"])
                    u.setdefault("view", {})["task"] = task.to_dict()
                    m["content"] = json.dumps(u)
            self.assertGreater(len(json.dumps(old)) - latest_chars, 30000)
            protected = {
                str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (root / "runs").rglob("*.json")
            }
            self.assertEqual((await backend.run(task, run_id="one")).to_dict(), result.to_dict())
            self.assertEqual(len(client.requests), 4)
            self.assertEqual(
                protected,
                {
                    str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in (root / "runs").rglob("*.json")
                },
            )

    async def test_latest_schema_view_changes_under_fixed_prompt_and_macro_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tools = file_tools(root / "files")
            profile = ExecutionProfile("state", tuple(t.name for t in tools))
            schema = {
                "nodes": [
                    {
                        "id": "State",
                        "source": "events",
                        "where": {"kind": "tool"},
                        "fields": {"output": {"path": "output", "type": "object"}},
                    }
                ],
                "relations": [],
            }
            assets = (
                AgentAsset("schema", "S", yaml.safe_dump(schema)),
                AgentAsset(
                    "current",
                    "F",
                    "def run(params):\n    return view()\n",
                    schema_dependencies=("schema",),
                    trial_inputs=({},),
                ),
                AgentAsset(
                    "worker",
                    "P",
                    "Fixed prompt. $instruction",
                    role="worker",
                    schema_dependencies=("schema",),
                ),
            )
            kernel = AgentKernelAssets(assets, profile).export(root / "K0")
            client = RawRecordedClient(
                [
                    json.dumps(
                        {"tool": "write_file", "params": {"path": "out", "content": "state"}}
                    ),
                    json.dumps({"tool": "current", "params": {}}),
                    json.dumps({"final_response": "done"}),
                ]
            )
            backend = ExecutionBackend(ReActAgent(), tools, profile, client, root / "runs")
            result = await backend.run(AgentTask("q", "inspect"), kernel, run_id="one")
            self.assertEqual(result.status, "completed")
            prompts, views = [], []
            for request in client.requests:
                users = [
                    json.loads(m["content"]) for m in request["messages"] if m["role"] == "user"
                ]
                prompts.append(users[0]["prompt"])
                self.assertEqual(sum("view" in u for u in users), 1)
                views.append(users[-1]["view"])
            self.assertEqual(len(set(prompts)), 1)
            self.assertEqual(views[0]["nodes"], [])
            self.assertEqual(len(views[1]["nodes"]), 1)
            observation = json.loads(client.requests[2]["messages"][-1]["content"])["observation"]
            self.assertEqual(observation["nodes"], views[1]["nodes"])
            self.assertEqual(observation["relations"], views[1]["relations"])
            self.assertEqual(views[2]["nodes"], views[1]["nodes"])
            self.assertEqual(
                observation,
                next(e["output"] for e in result.to_dict()["events"] if e["kind"] == "macro"),
            )
            changed = ExecutionBackend(
                replace(backend.adapter, role="judge"), tools, profile, client, root / "runs"
            )
            self.assertNotEqual(changed.identity(), backend.identity())
            with self.assertRaisesRegex(ValueError, "identity changed"):
                await changed.run(AgentTask("q", "inspect"), kernel, run_id="one")
