"""Synthetic full roster scheduling through real native execution and durable journals."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from darwinagent.config import Config
from datasets.lift.full import FullLocalRunner, SceneInputs
from datasets.lift.full_data import FullLiftDataset
from datasets.lift.transport import SharedTransport
from tests.support.full_lift_controller import RecordedFullClient
from tests.support.full_lift_data import full_lift_fixture


class FullControllerTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, root, **kwargs):
        root = root.resolve()
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        from datasets.lift.full_agents import full_initial_kernel

        full_lift_fixture(root / "data")
        dataset = FullLiftDataset(root / "data")
        cfg = Config(
            model_strong="synthetic",
            model_middle="synthetic",
            model_fast="synthetic",
            max_http_requests=None,
            work_dir=root / "transport",
        )
        cfg.role_tiers.update(worker="strong", judge="strong", trajectory_judge="strong")
        client = RecordedFullClient(cfg, **kwargs)
        shared = SharedTransport(client, root / "budget", worker_limit=None)
        initial = full_initial_kernel(root / "run" / "K0")
        client.initial = initial
        runner = FullLocalRunner(
            dataset=dataset,
            shared=shared,
            root=root / "run",
            initial=initial,
            run_id="synthetic-full",
            scenes=(dataset.scenes[0],),
        )
        return runner, client

    async def test_native_scoped_matrix_replay_and_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runner, client = self.fixture(root)
            with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
                result = await runner.run()
            self.assertEqual(result["selections"][0]["campaign"]["status"], "complete")
            self.assertTrue(result["selections"][0]["fallback"])
            proposals = [r for r in client.requests if r["role"] == "proposal"]
            self.assertEqual(len(proposals), 1)
            serialized = json.dumps(proposals)
            for forbidden in (
                "GOLD_CONTENT",
                "GOLD_TRAJECTORY",
                "Public query PPT制作攻略测评 q4",
                "Public query PPT制作攻略测评 q5",
                "Public query PPT制作攻略测评 q6",
            ):
                self.assertNotIn(forbidden, serialized)
            self.assertTrue(result["coverage_complete"])
            self.assertTrue(result["evaluation_healthy"], result["rows"])
            self.assertEqual(result["holdout_cells_settled"], 6)
            self.assertFalse(result["acceptance_complete"])
            holdout = [r for r in result["rows"] if r["phase"] == "holdout"]
            self.assertEqual(len({r["evidence_key"] for r in holdout}), 6)
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            calls = client.calls
            await runner.run()
            self.assertEqual(client.calls, calls)
            self.assertEqual(
                before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            )
            adapter = SceneInputs(runner.dataset, runner.scenes[0])
            with self.assertRaises(PermissionError):
                adapter.task(runner.dataset.task_ids(runner.scenes[0], "test")[0])

    async def test_known_failure_continues_and_false_success_unhealthy(self):
        for options in ({"fail_worker": True}, {"false_success": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                runner, client = self.fixture(Path(temporary), **options)
                if options.get("fail_worker"):
                    client.fail_worker = False
                    from src.lift.eval.task_exec import execute_task

                    failed = False

                    async def fail_once(**kwargs):
                        nonlocal failed
                        if not failed:
                            failed = True
                            raise ValueError("synthetic known local failure")
                        return await execute_task(**kwargs)

                    with patch("src.lift.eval.task_exec.execute_task", side_effect=fail_once):
                        result = await runner.run()
                else:
                    result = await runner.run()
                self.assertTrue(result["coverage_complete"])
                self.assertFalse(result["evaluation_healthy"])
                self.assertEqual(result["holdout_cells_settled"], 6)
                self.assertTrue(any(r["status"] == "failed" for r in result["rows"]))

    async def test_plan_all_cells_and_changed_config_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary))
            runner.scenes = tuple(runner.dataset.scenes)
            runner.plan()
            rows = [json.loads(p.read_text()) for p in (runner.root / "cells").glob("*.json")]
            self.assertEqual(len(rows), 140)
            self.assertEqual(sum(r["phase"] == "holdout" for r in rows), 84)
            self.assertEqual(client.calls, 0)
            with self.assertRaises(ValueError):
                FullLocalRunner(
                    dataset=runner.dataset,
                    shared=runner.shared,
                    root=runner.root,
                    initial=runner.initial,
                    run_id=runner.run_id,
                    rounds=2,
                    scenes=(runner.dataset.scenes[0],),
                )
            runner.scenes = (runner.dataset.scenes[0],)
            runner.shared.cfg.role_tiers["worker"] = "fast"
            with self.assertRaises(ValueError):
                runner.verify()

    async def test_unknown_global_gate_keeps_other_cells_unstarted(self):
        from darwinagent.llm.client import BudgetExceeded
        from darwinagent.runtime.steps import UnknownRequest

        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary))
            with patch(
                "src.lift.eval.task_exec.execute_task",
                side_effect=UnknownRequest("synthetic unknown"),
            ):
                with self.assertRaises(UnknownRequest):
                    await runner.run()
            rows = runner.write_report()["rows"]
            self.assertEqual(sum(r["status"] == "running" for r in rows), 1)
            self.assertEqual(sum(r["status"] == "not_started" for r in rows), 9)
            with self.assertRaises(BudgetExceeded):
                await runner.run()
            self.assertEqual(client.calls, 0)

    def test_full_native_identity_and_abc_tools_are_fixed(self):
        from datasets.lift.full_agents import FullNativeReAct, full_worker_profile
        from datasets.lift.full_tools import FullLocalTools

        identity = FullNativeReAct(max_turns=None, max_tokens=16384).identity()
        self.assertEqual(identity["native_correction_rounds"], 30)
        self.assertIsNone(identity["worker_session_cap"])
        self.assertIsNone(full_worker_profile().max_model_calls)
        self.assertEqual(len(full_worker_profile().capabilities), 7)
        with tempfile.TemporaryDirectory() as temporary:
            from datasets.lift.full_agents import FullDarwinChatAgent

            runner, client = self.fixture(Path(temporary))
            task = runner.dataset.task(runner.dataset.task_ids(runner.scenes[0], "test")[0])
            declarations = []
            for arm, kernel in (("A", None), ("B", runner.initial), ("C", runner.initial)):
                workspace = runner.root / arm
                agent = FullDarwinChatAgent(
                    task,
                    FullLocalTools(workspace).specs(),
                    runner.shared,
                    workspace / "session",
                    kernel,
                    role="worker",
                    run_id=runner.run_id,
                    session="synthetic-" + arm,
                    arm=arm,
                    phase="holdout",
                )
                declarations.append(
                    (agent.backend.profile.declaration(), agent.backend.adapter.identity())
                )
            self.assertEqual(declarations[0], declarations[1])
            self.assertEqual(declarations[1], declarations[2])
            self.assertEqual(len(FullLocalTools(runner.root, readonly=True).specs()), 6)

    async def test_native_thirtieth_round_and_adopted_bundle(self):
        from datasets.lift.full import execute_episode

        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary), thirty=True)
            task = runner.dataset.task(runner.dataset.task_ids(runner.scenes[0], "train")[0])
            runner.plan()
            episode, row = await execute_episode(runner, task, runner.initial, "campaign", "train")
            self.assertEqual(row["native"]["turns"], 30)
            self.assertTrue(row["healthy"], row["error"])
            self.assertEqual(client.judge_count, 30)
            requests = client.calls
            await execute_episode(runner, task, runner.initial, "campaign", "train")
            self.assertEqual(requests, client.calls)
        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary), adopt=True)
            result = await runner.run()
            selected = result["selections"][0]
            self.assertFalse(selected["fallback"], selected)
            self.assertNotEqual(selected["kernel_id"], runner.initial.content_id)
            for row in result["rows"]:
                if row["arm"] == "B":
                    self.assertEqual(row["kernel_id"], runner.initial.content_id)
                elif row["arm"] == "C":
                    self.assertEqual(row["kernel_id"], selected["kernel_id"])
            optimizer = json.dumps(
                [
                    r
                    for r in client.requests
                    if r["role"] not in ("worker", "judge", "trajectory_judge")
                ]
            )
            self.assertNotIn("Q4_PRIVATE_FEEDBACK_SENTINEL", optimizer)

    async def test_stage_failure_is_local_and_historical_requests_cannot_accept(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary))
            stage = runner.dataset.stage

            def fail_a(task, workspace):
                if task.id.endswith("-q5") and "b3f2ddcf1e1f4cfa5c0cc079" in str(workspace):
                    raise OSError("synthetic local stage failure")
                return stage(task, workspace)

            with patch.object(runner.dataset, "stage", side_effect=fail_a):
                result = await runner.run()
            self.assertTrue(result["coverage_complete"])
            self.assertEqual(result["holdout_cells_settled"], 6)
            self.assertEqual(sum(r["status"] == "failed" for r in result["rows"]), 1)
            runner.scenes = tuple(runner.dataset.scenes)
            runner.plan()
            from darwinagent.runtime.artifacts import atomic_json

            for path in (runner.root / "cells").glob("*.json"):
                row = json.loads(path.read_text())
                row.update(
                    status="completed", healthy=True, native={"success": True, "content_score": 1}
                )
                atomic_json(path, row)
            client.cfg.api_base_url = "https://dummy-recorded.invalid"
            client.http_attempts = lambda: 307
            atomic_json(
                runner.shared.root / "requests" / "historical.json",
                dict(
                    run_id="old-history",
                    role="worker",
                    status="completed",
                    actual_http_attempts=307,
                    usage={},
                    started=0,
                    finished=1,
                ),
            )
            result = runner.write_report()
            self.assertTrue(result["coverage_complete"])
            self.assertTrue(result["c_quality_satisfied"])
            self.assertFalse(result["acceptance_complete"])
            self.assertEqual(result["actual_http_attempts"], 307)
            self.assertEqual(result["current_run_http_attempts"], 0)

    async def test_sdk_error_never_persists_raw_body_or_key(self):
        import httpx
        from openai import APIStatusError

        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary))
            secret = "SYNTHETIC_API_KEY_MUST_NOT_PERSIST"
            response = httpx.Response(
                400, request=httpx.Request("POST", "https://synthetic.invalid")
            )
            error = APIStatusError(
                "raw request " + secret, response=response, body={"secret": secret}
            )
            error.transport_diagnostics = {"response_id": secret, "stage": "request"}
            with patch("src.lift.eval.task_exec.execute_task", side_effect=error):
                result = await runner.run()
            self.assertTrue(result["coverage_complete"])
            self.assertFalse(result["evaluation_healthy"])
            for path in runner.root.rglob("*.json"):
                self.assertNotIn(secret, path.read_text(), str(path))
            self.assertIn("APIStatusError", json.dumps(result))
            self.assertIn("http_status", json.dumps(result))

    def test_paused_shared_gate_cannot_accept_even_verified_healthy_full_matrix(self):
        from darwinagent.llm.client import BudgetExceeded
        from darwinagent.runtime.artifacts import atomic_json

        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary))
            runner.scenes = tuple(runner.dataset.scenes)
            runner.plan()
            for path in (runner.root / "cells").glob("*.json"):
                row = json.loads(path.read_text())
                row.update(
                    status="completed", healthy=True, native={"success": True, "content_score": 1}
                )
                atomic_json(path, row)
            with patch("datasets.lift.full_report.host_evidence", return_value=True):
                unpaused = runner.write_report()
                self.assertTrue(unpaused["acceptance_complete"])
                with patch.object(
                    runner.shared, "check", side_effect=BudgetExceeded("synthetic gate")
                ):
                    paused = runner.write_report()
            self.assertTrue(paused["coverage_complete"])
            self.assertTrue(paused["evaluation_healthy"])
            self.assertTrue(paused["c_quality_satisfied"])
            self.assertEqual(paused["status"], "paused")
            self.assertFalse(paused["acceptance_complete"])
            self.assertEqual(client.calls, 0)

    def test_cli_help_without_native_or_credentials(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        for key in ("DARWINAGENT_MODEL", "DARWINAGENT_BASE_URL", "DARWINAGENT_API_KEY"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-m", "datasets.lift.full", "--help"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
