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
    async def test_all_q5_arms_precede_any_q6_and_q5_fault_keeps_q6_unseen(self):
        import datasets.lift.full as full_module

        for fail_q5 in [False, True]:
            with self.subTest(fail_q5=fail_q5), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                original, client = self.fixture(root)
                runner = FullLocalRunner(
                    dataset=original.dataset,
                    shared=original.shared,
                    root=root / "two-scenes",
                    initial=original.initial,
                    run_id="two-scenes",
                    scenes=original.dataset.scenes[:2],
                )
                q5 = {runner.dataset.task_ids(s, "test")[0] for s in runner.scenes}
                q6 = {runner.dataset.task_ids(s, "test")[1] for s in runner.scenes}
                order = []
                execute = full_module.execute_episode
                stage = runner.dataset.stage
                failed = False

                async def observe(
                    owner, task, kernel, arm, phase, *, _order=order, _execute=execute
                ):
                    if phase == "holdout":
                        _order.append(task.id)
                    return await _execute(owner, task, kernel, arm, phase)

                def stage_or_fail(task, workspace, *, _fail=fail_q5, _q5=q5, _stage=stage):
                    nonlocal failed
                    if _fail and task.id in _q5 and not failed:
                        failed = True
                        raise ValueError("synthetic q5 setup failure")
                    return _stage(task, workspace)

                with (
                    patch.object(full_module, "execute_episode", side_effect=observe),
                    patch.object(runner.dataset, "stage", side_effect=stage_or_fail),
                ):
                    result = await runner.run()
                self.assertEqual(sum(t in q5 for t in order), 6)
                if fail_q5:
                    self.assertFalse(any(t in q6 for t in order))
                    self.assertFalse(result["q6_confirmation_ready"])
                    self.assertFalse(result["coverage_complete"])
                else:
                    self.assertEqual(sum(t in q6 for t in order), 6)
                    self.assertLess(
                        max(i for i, t in enumerate(order) if t in q5),
                        min(i for i, t in enumerate(order) if t in q6),
                    )
                    self.assertTrue(result["q6_confirmation_ready"])

    async def test_controlled_policy_keeps_native_scoring_and_complete_trajectory(self):
        from datasets.lift.task_budget import TaskExecutionPolicy
        from datasets.lift.task_control import TaskControlPolicy

        with tempfile.TemporaryDirectory() as tmp:
            runner, client = self.fixture(
                Path(tmp), task_policy=TaskExecutionPolicy(3, 24, control=TaskControlPolicy())
            )
            result = await runner.run()
            self.assertTrue(result["coverage_complete"])
            self.assertTrue(result["evaluation_healthy"])
            self.assertEqual(runner.identity()["task_execution_policy"]["kind"], "bounded-task-v2")
            workers = [r for r in client.requests if r["role"] == "worker"]
            self.assertTrue(workers)
            for request in workers:
                snapshot = json.loads(request["messages"][-1]["content"])["execution_control"]
                self.assertEqual(snapshot["task_model"]["limit"], 24)
                self.assertEqual(snapshot["policy"]["retrieval_limit"], 12)

    async def test_completed_content_failures_reach_evolution_without_validation_feedback(self):
        from datasets.lift.task_budget import TaskExecutionPolicy
        from datasets.lift.task_control import TaskControlPolicy

        with tempfile.TemporaryDirectory() as tmp:
            runner, client = self.fixture(
                Path(tmp),
                thirty=True,
                task_policy=TaskExecutionPolicy(3, 24, control=TaskControlPolicy()),
            )
            original_chat = client.chat

            async def distinguish_feedback(**kwargs):
                reply = await original_chat(**kwargs)
                if kwargs["role"] == "judge":
                    body = json.loads(reply.content)
                    if "final_response" in body:
                        task = json.loads(kwargs["messages"][1]["content"])["task"]["id"]
                        result = json.loads(body["final_response"])
                        result["reason"] = (
                            "PRIVATE_VALIDATION_FAILURE"
                            if task.endswith("-q4")
                            else "PUBLIC_TRAINING_FAILURE"
                        )
                        body["final_response"] = json.dumps(result)
                        reply.content = json.dumps(body)
                return reply

            with (
                patch.object(client, "chat", side_effect=distinguish_feedback),
                patch("socket.socket.connect", side_effect=AssertionError("offline only")),
            ):
                selected = await runner.evolve(runner.scenes[0])
            rows = [
                json.loads(path.read_text())["row"]
                for path in (runner.root / "episodes").glob("*/receipt.json")
            ]
            self.assertTrue(rows)
            self.assertTrue(all(row["healthy"] for row in rows))
            self.assertTrue(all(not row["native"]["success"] for row in rows))
            learning = [
                r
                for r in client.requests
                if r["role"] not in {"worker", "judge", "trajectory_judge"}
            ]
            self.assertTrue(any(r["role"] == "proposal" for r in learning))
            payload = json.dumps(learning)
            self.assertIn("PUBLIC_TRAINING_FAILURE", payload)
            for private in ("PRIVATE_VALIDATION_FAILURE", "GOLD_CONTENT", "GOLD_TRAJECTORY"):
                self.assertNotIn(private, payload)
            self.assertEqual(selected.content_id, runner.initial.content_id)
            self.assertEqual(client.http_attempts(), 0)

    def fixture(self, root, *, task_policy=None, resource_guard=None, **kwargs):
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
        if resource_guard is not None:
            client.resource_guard = resource_guard
        shared = SharedTransport(client, root / "budget", worker_limit=None)
        shared.generation_max_tokens = 16384
        initial = full_initial_kernel(root / "run" / "K0")
        client.initial = initial
        runner = FullLocalRunner(
            dataset=dataset,
            shared=shared,
            root=root / "run",
            initial=initial,
            run_id="synthetic-full",
            scenes=(dataset.scenes[0],),
            task_policy=task_policy,
        )
        return runner, client

    async def test_resource_pressure_leaves_unstarted_cells_and_can_resume(self):
        from darwinagent.llm.client import ResourceBudgetExceeded
        from darwinagent.runtime.resources import ResourceGuard, ResourceLimit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            guard = ResourceGuard(root, reserve_bytes=4096, min_free_bytes=0, max_rss_bytes=None)
            guard.prepare()
            try:
                runner, client = self.fixture(root, resource_guard=guard)
                with patch.object(guard, "check", side_effect=ResourceLimit("synthetic pressure")):
                    with self.assertRaises(ResourceBudgetExceeded):
                        await runner.run()
                self.assertEqual(client.calls, 0)
                cells = [json.loads(p.read_text()) for p in (runner.root / "cells").glob("*.json")]
                self.assertTrue(cells)
                self.assertTrue(all(c["status"] == "not_started" for c in cells))
                self.assertFalse((runner.shared.root / "active-pause.json").exists())
                runner.shared.check()
                report = json.loads((runner.root / "full-matrix-report.json").read_text())
                self.assertEqual(report["status"], "paused")
                self.assertFalse(report["acceptance_complete"])
            finally:
                guard.close()

    async def test_real_factory_releases_reserve_if_enrollment_or_setup_fails(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from datasets.lift.full_config import build_runner
        from datasets.lift.full_resources import DEFAULT_LIMITS

        with tempfile.TemporaryDirectory() as tmp:
            config = dict(
                model="MiniMax-M3.1-Flash-Preview",
                base_url="https://api.minimax.cn/v1",
                temperature=0,
                correction_rounds=30,
                max_tokens=16384,
                worker_limit=None,
                http_cap=None,
                provider_request_policy="minimax-adaptive-v1",
                unbounded_model_calls=True,
                budget_root=tmp,
                run_root=tmp,
                resource_limits=dict(DEFAULT_LIMITS),
            )
            environment = {
                "DARWINAGENT_MODEL": config["model"],
                "DARWINAGENT_BASE_URL": config["base_url"],
                "DARWINAGENT_API_KEY": "offline-dummy",
            }
            for phase in ["enrollment", "setup"]:
                guard = SimpleNamespace(close=Mock())
                with (
                    self.subTest(phase=phase),
                    patch.dict(os.environ, environment),
                    patch("datasets.lift.full_config.LLMClient", return_value=SimpleNamespace()),
                    patch("datasets.lift.full_resources.prepare_resources", return_value=guard),
                    patch(
                        "datasets.lift.full_transport.FullSharedTransport",
                        side_effect=ValueError("enrollment") if phase == "enrollment" else None,
                    ),
                    patch(
                        "datasets.lift.full_config._finish_runner", side_effect=ValueError("setup")
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, phase):
                        build_runner(config, "factory-failure")
                    guard.close.assert_called_once()

    async def test_optional_task_policy_uses_three_rounds_with_unchanged_full_coverage(self):
        from datasets.lift.full import FullEvaluator, execute_episode
        from datasets.lift.task_budget import TaskExecutionPolicy

        with tempfile.TemporaryDirectory() as tmp:
            runner, client = self.fixture(
                Path(tmp), thirty=True, task_policy=TaskExecutionPolicy(3, 8)
            )
            runner.scenes = tuple(runner.dataset.scenes)
            runner.plan()
            cells = [json.loads(p.read_text()) for p in (runner.root / "cells").glob("*.json")]
            self.assertEqual(len(cells), 140)
            self.assertEqual(sum(c["phase"] == "holdout" for c in cells), 84)
            runner.scenes = (runner.dataset.scenes[0],)
            task = runner.dataset.task(runner.dataset.task_ids(runner.scenes[0], "train")[0])
            _, row = await execute_episode(runner, task, runner.initial, "campaign", "train")
            self.assertTrue(row["healthy"], row.get("diagnostics"))
            self.assertEqual(row["native"]["turns"], 3)
            self.assertEqual(client.judge_count, 3)
            self.assertEqual(
                FullEvaluator(runner, runner.scenes[0]).declaration()["task_execution_policy"],
                TaskExecutionPolicy(3, 8).declaration(),
            )
            calls = client.calls
            runner.task_policy = TaskExecutionPolicy(3, 9)
            with self.assertRaises(ValueError):
                runner.verify()
            self.assertEqual(client.calls, calls)

    async def test_configuration_loader_binds_explicit_task_policy(self):
        from datasets.lift.full_config import build_runner
        from datasets.lift.task_budget import TaskExecutionPolicy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runner, client = self.fixture(root)
            config = dict(
                model="MiniMax-M3.1-Flash-Preview",
                base_url="https://api.minimax.cn/v1",
                temperature=0,
                correction_rounds=3,
                max_tokens=16384,
                worker_limit=None,
                http_cap=None,
                provider_request_policy="minimax-adaptive-v1",
                unbounded_model_calls=True,
                data_root=str(root / "data"),
                run_root=str(root / "bounded"),
                task_execution_policy=TaskExecutionPolicy(3, 8).declaration(),
                scenes=list(runner.scenes),
            )
            bounded = build_runner(config, "bounded-config", shared=runner.shared)
            self.assertEqual(bounded.correction_rounds, 3)
            self.assertEqual(bounded.task_policy, TaskExecutionPolicy(3, 8))
            self.assertEqual(bounded.identity()["protocol"], "full-lift-local-bounded-v1")
            self.assertEqual(client.calls, 0)
            with self.assertRaises(ValueError):
                build_runner(dict(config, correction_rounds=30), "mismatch", shared=runner.shared)

    async def test_native_calendar_tamper_refuses_terminal_replay_without_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runner, client = self.fixture(root)
            result = await runner.run()
            row = next(row for row in result["rows"] if row.get("healthy"))
            self.assertTrue(row.get("native_calendar"), "Terminal receipt must bind its calendar")
            path = root / "run/episodes" / row["evidence_key"] / "native-date.json"
            record = json.loads(path.read_text())
            record["date"] = "2000-01-01"
            path.write_text(json.dumps(record))
            calls = client.calls
            with self.assertRaises(ValueError):
                await runner.run()
            self.assertEqual(client.calls, calls)

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
            for row in result["rows"]:
                if row.get("healthy"):
                    judge_input = (
                        runner.root / "episodes" / row["evidence_key"] / "trajectory/input.json"
                    )
                    self.assertEqual(
                        json.loads(judge_input.read_text())["schema"], "full-lift-judge-input-v6"
                    )
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
                self.assertEqual(
                    result["coverage_complete"], not options.get("false_success", False)
                )
                self.assertFalse(result["evaluation_healthy"])
                self.assertEqual(
                    result["holdout_cells_settled"], 3 if options.get("false_success") else 6
                )
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
        self.assertEqual(
            full_worker_profile().capabilities,
            (
                "list_files",
                "read_text",
                "read_table",
                "transform_tables",
                "write_text",
                "replace_text",
                "search_web",
                "read_web",
            ),
        )
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
            judge_specs = FullLocalTools(runner.root, readonly=True).specs()
            self.assertEqual(
                tuple(t.name for t in judge_specs),
                (
                    "list_files",
                    "read_text",
                    "read_table",
                    "transform_tables",
                    "search_web",
                    "read_web",
                ),
            )
            self.assertTrue(all(t.effect == "read" for t in judge_specs))

    async def test_native_thirtieth_round_and_adopted_bundle(self):
        from datasets.lift.full import execute_episode

        with tempfile.TemporaryDirectory() as temporary:
            runner, client = self.fixture(Path(temporary), thirty=True)
            task = runner.dataset.task(runner.dataset.task_ids(runner.scenes[0], "train")[0])
            runner.plan()
            episode, row = await execute_episode(runner, task, runner.initial, "campaign", "train")
            self.assertEqual(row["native"]["turns"], 30)
            self.assertTrue(row["healthy"], row.get("diagnostics") or row["error"])
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
            self.assertFalse(result["coverage_complete"])
            self.assertFalse(result["q6_confirmation_ready"])
            self.assertEqual(result["holdout_cells_settled"], 3)
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
            self.assertFalse(result["coverage_complete"])
            self.assertFalse(result["q6_confirmation_ready"])
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
        # Validate this checkout's core, without the pinned native LIFT package
        # or an unrelated editable installation shadowing the new API.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
        for key in ("DARWINAGENT_MODEL", "DARWINAGENT_BASE_URL", "DARWINAGENT_API_KEY"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-m", "datasets.lift.full", "--help"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
