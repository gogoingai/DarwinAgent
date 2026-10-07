import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.engine import Pipeline
from darwinagent.runtime.execution import ActiveBudget, ExecutionSelection
from darwinagent.runtime.steps import StepJournal, UnknownRequest
from darwinagent.runtime.workspace import Workspace
from tests.support.device import case, client, spec


class Continuation(unittest.TestCase):
    def test_public_fork_lazily_reuses_parent_without_calls(self):
        from darwinagent.cli import main
        from darwinagent.experiments.control import preview

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            workspace = Workspace(root / "workspace")
            first = asyncio.run(
                Pipeline(client(c), root / "generation", workspace=workspace).run(
                    c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            source = next((root / "generation" / c.id / "branches/main/answers").glob("*.json"))
            original = source.read_bytes()
            self.assertEqual(main(["workspace", "--root", str(root), "fork", "alternative"]), 0)
            selection = ExecutionSelection(mode="fork", branch="alternative", stages=("answer",))
            target = root / "generation" / c.id / "branches/alternative/answers"
            plan = preview(root, [c], selection)
            self.assertEqual(len(plan.reuse), 1)
            self.assertFalse(plan.execute)
            self.assertFalse(target.exists())
            complete_plan = preview(
                root, [c], replace(selection, stages=ExecutionSelection().stages)
            )
            self.assertEqual([item["stage"] for item in complete_plan.execute], ["score"])
            transport = client(c)
            forked = asyncio.run(
                Pipeline(transport, root / "generation", workspace=workspace).run(
                    c, task, RunConfig(protocol_attempts=1), execution=selection
                )
            )
            self.assertFalse(transport.calls)
            self.assertEqual(forked.answers, first.answers)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(list(target.glob("*.json"))), 1)
            # An unmaterialized intermediate fork can still inherit the original progress.
            workspace.create_branch("descendant", parent="alternative")
            inherited = asyncio.run(
                Pipeline(transport, root / "generation", workspace=workspace).run(
                    c,
                    task,
                    RunConfig(protocol_attempts=1),
                    execution=ExecutionSelection(branch="descendant", stages=("answer",)),
                )
            )
            self.assertEqual(inherited.answers, first.answers)
            self.assertFalse(transport.calls)

    def test_changed_concurrency_reuses_complete_and_retains_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c, transport = case(), client(case())
            task = spec(root / "assets")
            pipeline = Pipeline(transport, root / "generation")
            first = asyncio.run(
                pipeline.run(
                    c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            calls = len(transport.calls)
            second = asyncio.run(
                pipeline.run(
                    c,
                    task,
                    RunConfig(concurrency=1, protocol_attempts=1),
                    execution=ExecutionSelection(),
                )
            )
            self.assertEqual(len(transport.calls), calls)
            self.assertEqual(first.answers, second.answers)
            saved = next((root / "generation" / c.id / "branches/main/answers").glob("*.json"))
            self.assertEqual(json.loads(saved.read_text())["identity"], first.identity)
            self.assertNotEqual(first.identity, second.identity)

    def test_question_text_version_does_not_reuse_old_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            asyncio.run(
                Pipeline(client(c), root / "generation").run(
                    c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            changed = replace(c, questions=(replace(c.questions[0], text="维护人是谁？"),))
            transport = client(c)
            asyncio.run(
                Pipeline(transport, root / "generation").run(
                    changed, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            self.assertEqual(
                [x["role"] for x in transport.calls], ["tools", "tools", "answer", "review"]
            )
            self.assertEqual(
                len(list((root / "generation" / c.id / "branches/main/answers").glob("*.json"))), 2
            )

    def test_failed_retry_preserves_old_result_and_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            failed = asyncio.run(
                Pipeline(client(c, answers=["bad"]), root / "generation").run(
                    c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            self.assertEqual(failed.answers[0].status, "execution_error")
            transport = client(c)
            result = asyncio.run(
                Pipeline(transport, root / "generation").run(
                    c,
                    task,
                    RunConfig(protocol_attempts=1),
                    execution=ExecutionSelection(mode="retry_failed"),
                )
            )
            self.assertEqual(result.answers[0].status, "answered")
            self.assertTrue(list((root / "generation" / c.id).glob("preparations/*/history/**/*")))
            self.assertFalse(any(x["use_cache"] for x in transport.calls))

    def test_no_hidden_preparation_when_graph_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c, transport = case(), client(case())
            with self.assertRaisesRegex(ValueError, "Missing graph"):
                asyncio.run(
                    Pipeline(transport, root / "generation").run(
                        c,
                        spec(root / "assets"),
                        RunConfig(),
                        execution=ExecutionSelection(stages=("answer",)),
                    )
                )
            self.assertFalse(transport.calls)

    def test_saved_steps_replay_and_unknown_never_resends(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = StepJournal(tmp)
            path, _ = journal.reserve("model", {"role": "answer"})
            journal.submitted(path)
            with self.assertRaises(UnknownRequest):
                StepJournal(tmp).reserve("model", {"role": "answer"})
            journal.respond(path, {"content": "answer"})
            self.assertEqual(
                StepJournal(tmp).reserve("model", {"role": "answer"})[1]["content"], "answer"
            )
            tool = StepJournal(Path(tmp) / "tools")
            tool.tool({"x": 1}, lambda: {"full": [1, 2, 3]})
            self.assertEqual(
                StepJournal(Path(tmp) / "tools").tool({"x": 1}, lambda: self.fail("reexecuted")),
                {"full": [1, 2, 3]},
            )

    def test_offline_time_not_consumed_and_budget_can_increase(self):
        with tempfile.TemporaryDirectory() as tmp:
            times = iter([0, 3, 1000, 1002])

            def clock():
                return next(times)

            path = Path(tmp) / "budget.json"
            with ActiveBudget(path, 10, clock):
                pass
            with ActiveBudget(path, 20, clock):
                pass
            self.assertEqual(json.loads(path.read_text())["spent_s"], 5)

    def test_answer_without_review_is_marked_and_check_only_has_no_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            transport = client(c)
            config = RunConfig(protocol_attempts=1)
            result = asyncio.run(
                Pipeline(transport, root / "generation").run(
                    c,
                    task,
                    config,
                    execution=ExecutionSelection(stages=("graph", "retrieval", "answer", "check")),
                )
            )
            self.assertNotIn("review", [x["role"] for x in transport.calls])
            self.assertTrue(
                any(
                    e.get("stage") == "review" and e.get("status") == "not_selected"
                    for e in result.answers[0].trace
                )
            )
            new_client = client(c)
            checked = asyncio.run(
                Pipeline(new_client, root / "generation").run(
                    c, task, config, execution=ExecutionSelection(mode="rerun", stages=("check",))
                )
            )
            self.assertEqual(checked.answers[0].status, "answered")
            self.assertFalse(new_client.calls)

    def test_retrieval_only_does_not_generate_answer_or_replace_old_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            transport = client(c)
            result = asyncio.run(
                Pipeline(transport, root / "generation").run(
                    c,
                    task,
                    RunConfig(protocol_attempts=1),
                    execution=ExecutionSelection(stages=("graph", "retrieval")),
                )
            )
            self.assertFalse(result.answers)
            self.assertEqual([x["role"] for x in transport.calls], ["extraction", "tools", "tools"])
            next_client = client(c)
            result = asyncio.run(
                Pipeline(next_client, root / "generation").run(
                    c,
                    task,
                    RunConfig(protocol_attempts=1),
                    execution=ExecutionSelection(stages=("answer", "check")),
                )
            )
            self.assertEqual(result.answers[0].status, "answered")
            self.assertEqual([x["role"] for x in next_client.calls], ["answer"])

    def test_eighty_success_forty_pending_only_forty_new_answers(self):
        from darwinagent.llm.recorded import RecordedClient
        from tests.support.device import extraction, review

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            task = spec(root / "assets")
            questions = tuple(replace(c.questions[0], id=str(i)) for i in range(120))

            def replies(count, include_extraction=False):
                return RecordedClient(
                    {
                        "extraction": [extraction(c)] if include_extraction else [],
                        "tools": [
                            {
                                "action": "call",
                                "asset_id": "device_lookup",
                                "parameters": {"serial": "D-17"},
                            },
                            {"action": "ready"},
                        ]
                        * count,
                        "answer": [
                            {
                                "status": "answered",
                                "answer": "林于2026-09-01维护。",
                                "node_ids": ["n000000"],
                            }
                        ]
                        * count,
                        "review": [review()] * count,
                    }
                )

            asyncio.run(
                Pipeline(replies(80, True), root / "generation").run(
                    replace(c, questions=questions[:80]),
                    task,
                    RunConfig(concurrency=1, protocol_attempts=1),
                    execution=ExecutionSelection(),
                )
            )
            transport = replies(40)
            result = asyncio.run(
                Pipeline(transport, root / "generation").run(
                    replace(c, questions=questions),
                    task,
                    RunConfig(concurrency=1, protocol_attempts=1),
                    execution=ExecutionSelection(),
                )
            )
            self.assertEqual(len(result.answers), 120)
            self.assertEqual(sum(x["role"] == "answer" for x in transport.calls), 40)
            self.assertNotIn("extraction", [x["role"] for x in transport.calls])
            self.assertEqual(len(transport.calls), 160)
