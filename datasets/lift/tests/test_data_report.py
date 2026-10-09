"""Offline boundary and native multi-turn failure accounting regressions."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from darwinagent.agent import AgentTask, EpisodeResult
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.data import LiftDataset, sha
from datasets.lift.report import cell, cell_rows, update_cell

try:
    import src.lift  # noqa: F401
except ModuleNotFoundError:
    NATIVE_AVAILABLE = False
else:
    NATIVE_AVAILABLE = True


@unittest.skipUnless(NATIVE_AVAILABLE, "Pinned native LIFT environment required")
class MaterialTests(unittest.TestCase):
    def test_extra_file_and_escaping_symlink_refused_before_dispatch(self):
        import asyncio

        from datasets.lift.adapter import DarwinLiftAdapter

        for attack in ("extra", "symlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                material = root / "q1_materials"
                material.mkdir()
                (material / "allowed.txt").write_text("public")
                dataset = LiftDataset.__new__(LiftDataset)
                dataset.root = root
                dataset.files = [
                    {"path": "q1_materials/allowed.txt", "sha256": sha(material / "allowed.txt")}
                ]
                dataset.native = {
                    "q1": SimpleNamespace(
                        query="query", requirements=SimpleNamespace(material_dir=material)
                    )
                }
                dataset.materials = {"q1": {"allowed.txt": sha(material / "allowed.txt")}}
                task = dataset.task("q1")
                if attack == "extra":
                    (material / "unlisted.txt").write_text("not pinned")
                else:
                    private = root / "private.txt"
                    private.write_text("must not read")
                    (material / "unlisted.txt").symlink_to(private)
                with self.assertRaises(ValueError):
                    dataset.stage(task, root / "staged")
                self.assertFalse((root / "staged").exists())
                owner = DarwinLiftAdapter.__new__(DarwinLiftAdapter)
                owner.dataset, owner.shared = dataset, SimpleNamespace(check=lambda: None)
                with patch(
                    "datasets.lift.adapter.ContainerSession.start", new_callable=AsyncMock
                ) as dispatch:
                    with self.assertRaises(ValueError):
                        asyncio.run(
                            owner.start_container(
                                instance_id="test",
                                image="none",
                                ctx=None,
                                workspace_dir=root / "workspace",
                                seed_workspace=True,
                                task=None,
                            )
                        )
                    dispatch.assert_not_awaited()


def episode(status, models, leaves):
    return EpisodeResult(
        status,
        "answer",
        (),
        ({"kind": "model", "usage": {"prompt_tokens": 10, "completion_tokens": 2}},) * models,
        {"model_calls": models, "leaf_calls": leaves, "unknown_calls": 0, "pending_calls": 0},
    )


@unittest.skipUnless(NATIVE_AVAILABLE, "Pinned native LIFT environment required")
class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def recorded_agent(self, root, role, results):
        from datasets.lift.agents import DarwinChatAgent

        agent = DarwinChatAgent.__new__(DarwinChatAgent)
        agent.root, agent.role, agent.session, agent.run_id = root, role, role + "-session", "run"
        agent.cursor, agent.kernel, agent.trace = 0, None, False
        agent.task = AgentTask("q5", "query")
        agent.shared = SimpleNamespace(check=lambda: None, pause=lambda reason: None)
        agent.backend = SimpleNamespace(run=AsyncMock(side_effect=results))
        return agent

    async def test_worker_second_turn_failure_preserves_all_costs_once(self):
        for failure in (episode("budget_exhausted", 2, 1), RuntimeError("before result")):
            with (
                self.subTest(failure=type(failure).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                key = cell(
                    root,
                    "q5",
                    "B",
                    "holdout",
                    "run",
                    status="running",
                    started=1.0,
                    sessions=["worker-session"],
                )
                agent = await self.recorded_agent(
                    root / "sessions/worker-session",
                    "worker",
                    [episode("completed", 4, 4), failure],
                )
                with patch("datasets.lift.agents.tracing.emit"):
                    await agent.chat("first", session_id=agent.session)
                    with self.assertRaises((BudgetExceeded, RuntimeError)):
                        await agent.chat("correction", session_id=agent.session)
                update_cell(root, key, status="paused", finished=3.0)
                # Duplicated legacy latest receipt must not be counted again.
                atomic_json(agent.root / "last-episode.json", episode("failed", 99, 99).to_dict())
                row = cell_rows(root)[0]
                self.assertEqual(
                    (row["arm"], row["phase"], row["status"]), ("B", "holdout", "paused")
                )
                self.assertEqual(
                    row["worker_usage"]["leaf_calls"],
                    5 if isinstance(failure, EpisodeResult) else None,
                )
                self.assertEqual(
                    row["worker_usage"]["model_calls"],
                    6 if isinstance(failure, EpisodeResult) else None,
                )
                self.assertEqual(row["correction_turns"], 1)
                self.assertEqual(row["worker_unfinished_turns"], 1)
                self.assertEqual(row["judge_usage"]["model_calls"], 0)
                self.assertEqual(row["task_time_s"], 2)

    async def test_judge_failure_and_unstarted_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = cell(
                root,
                "q5",
                "B",
                "holdout",
                "run",
                status="running",
                sessions=["worker-session", "judge-session"],
            )
            cell(root, "q5", "C", "holdout", "run")
            worker = await self.recorded_agent(
                root / "sessions/worker-session", "worker", [episode("completed", 4, 4)]
            )
            judge = await self.recorded_agent(
                root / "sessions/judge-session", "judge", [episode("failed", 2, 1)]
            )
            with patch("datasets.lift.agents.tracing.emit"):
                await worker.chat("first", session_id=worker.session)
                with self.assertRaises(RuntimeError):
                    await judge.chat("judge", session_id=judge.session)
            update_cell(root, key, status="failed")
            rows = {r["arm"]: r for r in cell_rows(root)}
            self.assertEqual(rows["B"]["worker_usage"]["leaf_calls"], 4)
            self.assertEqual(rows["B"]["judge_usage"]["leaf_calls"], 1)
            self.assertEqual(rows["B"]["worker_tokens"], 48)
            self.assertEqual(rows["B"]["judge_tokens"], 24)
            self.assertEqual(rows["C"]["status"], "unstarted")
            self.assertEqual(rows["C"]["worker_turns"], 0)
            self.assertIsNone(rows["C"]["task_time_s"])

    async def test_failure_before_dispatch_still_has_arm_row(self):
        from datasets.lift.adapter import DarwinLiftAdapter

        with tempfile.TemporaryDirectory() as directory:
            owner = DarwinLiftAdapter.__new__(DarwinLiftAdapter)
            owner.root = Path(directory)
            owner.shared = SimpleNamespace(check=lambda: None)
            owner.dataset = SimpleNamespace(
                identity=lambda: (_ for _ in ()).throw(ValueError("changed"))
            )
            operation = AsyncMock()
            with self.assertRaises(ValueError):
                await owner.track("q5", "A", "holdout", "run", operation)
            operation.assert_not_awaited()
            row = cell_rows(owner.root)[0]
            self.assertEqual((row["arm"], row["status"], row["worker_turns"]), ("A", "failed", 0))


class TokenCompletenessTests(unittest.TestCase):
    def test_unknown_first_and_second_requests_for_each_role(self):
        for role in ("worker", "judge"):
            for completed in (0, 1):
                with (
                    self.subTest(role=role, completed=completed),
                    tempfile.TemporaryDirectory() as d,
                ):
                    root = Path(d)
                    session = role + "-session"
                    cell(root, "q5", "B", "holdout", "run", status="paused", sessions=[session])
                    events = [
                        {"kind": "model", "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
                    ] * completed
                    result = EpisodeResult(
                        "unknown",
                        events=tuple(events),
                        usage={
                            "model_calls": completed + 1,
                            "leaf_calls": 0,
                            "unknown_calls": 1,
                            "pending_calls": 1,
                        },
                    )
                    atomic_json(
                        root / "sessions" / session / "turns/0.json",
                        {"status": "unknown", "episode": result.to_dict()},
                    )
                    row = cell_rows(root)[0]
                    self.assertIsNone(row[role + "_tokens"])
                    self.assertEqual(row[role + "_known_tokens_subtotal"], 13 * completed)
                    self.assertEqual(row[role + "_unknown_token_requests"], 1)
                    self.assertEqual(row[role + "_usage"]["model_calls"], completed + 1)
                    self.assertEqual(row[role + "_accounting_status"], "incomplete")

    def test_unfinished_without_result_reads_durable_steps_for_each_role(self):
        for role in ("worker", "judge"):
            for completed in (0, 1):
                with (
                    self.subTest(role=role, completed=completed),
                    tempfile.TemporaryDirectory() as d,
                ):
                    root = Path(d)
                    session = role + "-session"
                    cell(root, "q5", "B", "holdout", "run", status="running", sessions=[session])
                    directory = root / "sessions" / session
                    atomic_json(directory / "turns/0.json", {"status": "running", "episode": None})
                    if completed:
                        atomic_json(
                            directory / "episodes/turn-0/steps/000000.json",
                            {
                                "kind": "model",
                                "state": "responded",
                                "response": {
                                    "content": "ok",
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                                },
                            },
                        )
                    atomic_json(
                        directory / f"episodes/turn-0/steps/{completed:06d}.json",
                        {"kind": "model", "state": "submitted"},
                    )
                    before = {p: p.read_bytes() for p in directory.rglob("*.json")}
                    row = cell_rows(root)[0]
                    self.assertEqual(row[role + "_usage"]["model_calls"], completed + 1)
                    self.assertEqual(row[role + "_usage"]["unknown_calls"], 1)
                    self.assertIsNone(row[role + "_tokens"])
                    self.assertEqual(row[role + "_known_tokens_subtotal"], completed * 13)
                    self.assertEqual(row[role + "_unknown_token_requests"], 1)
                    self.assertEqual(before, {p: p.read_bytes() for p in directory.rglob("*.json")})

    def test_missing_result_and_receipts_are_unavailable_not_unstarted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions = [role + "-session" for role in ("worker", "judge")]
            cell(root, "q5", "B", "holdout", "run", status="paused", sessions=sessions)
            cell(root, "q5", "C", "holdout", "run")
            for session in sessions:
                atomic_json(
                    root / "sessions" / session / "turns/0.json",
                    {"status": "running", "episode": None},
                )
            rows = {r["arm"]: r for r in cell_rows(root)}
            for role in ("worker", "judge"):
                self.assertIsNone(rows["B"][role + "_usage"]["model_calls"])
                self.assertIsNone(rows["B"][role + "_tokens"])
                self.assertEqual(rows["B"][role + "_accounting_status"], "incomplete")
                self.assertEqual(rows["C"][role + "_usage"]["model_calls"], 0)
                self.assertEqual(rows["C"][role + "_tokens"], 0)
                self.assertEqual(rows["C"][role + "_accounting_status"], "unstarted")

    def test_reservation_response_event_correspondence(self):
        from darwinagent.runtime.artifacts import digest

        for role in ("worker", "judge"):
            for variant in ("complete", "missing_usage", "missing_response", "mismatched_event"):
                with self.subTest(role=role, variant=variant), tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    session = role + "-session"
                    cell(root, "q5", "B", "holdout", "run", status="completed", sessions=[session])
                    directory = root / "sessions" / session
                    usage = (
                        {}
                        if variant == "missing_usage"
                        else {"prompt_tokens": 10, "completion_tokens": 3}
                    )
                    event = {
                        "kind": "model",
                        "usage": usage,
                        "response_digest": digest(
                            "other" if variant == "mismatched_event" else "ok"
                        ),
                    }
                    result = EpisodeResult(
                        "completed",
                        events=(event,),
                        usage={
                            "model_calls": 1,
                            "leaf_calls": 0,
                            "unknown_calls": 0,
                            "pending_calls": 0,
                        },
                    )
                    atomic_json(
                        directory / "turns/0.json",
                        {"status": "completed", "episode": result.to_dict()},
                    )
                    atomic_json(
                        directory / "episodes/turn-0/steps/000000.json",
                        {
                            "kind": "model",
                            "state": "submitted" if variant == "missing_response" else "responded",
                            "response": {"content": "ok", "usage": usage},
                        },
                    )
                    row = cell_rows(root)[0]
                    self.assertEqual(row[role + "_tokens"], 13 if variant == "complete" else None)
