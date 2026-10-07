"""No network: distinguish rejected budget, known failure, and unknown dispatch."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import APITimeoutError

from darwinagent.agents.protocol import ModelSession
from darwinagent.config import Config, RunConfig
from darwinagent.llm.client import BudgetExceeded, LLMClient
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.execution import ActiveBudget
from darwinagent.runtime.steps import AwaitingBudget, StepJournal, UnknownRequest
from darwinagent.runtime.workspace import Workspace


class DurableDispatchTests(unittest.TestCase):
    def test_client_durable_timeout_is_one_submission_not_internal_retry(self):
        async def run(root):
            cfg = Config(
                api_base_url="http://unused.invalid/v1",
                api_key="fixture",
                model_strong="fixture",
                work_dir=root,
                max_retries=10,
            )
            transport = LLMClient(cfg)
            create = AsyncMock(
                side_effect=APITimeoutError(request=httpx.Request("POST", "http://unused.invalid"))
            )
            transport._site_for = lambda model: (
                SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                asyncio.Semaphore(1),
            )
            try:
                with self.assertRaises(APITimeoutError):
                    await transport.chat(role="answer", messages=[], durable=True, use_cache=False)
                self.assertEqual(create.await_count, 1)
            finally:
                await transport.aclose()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_predispatch_budget_retains_prepared_request_then_continues(self):
        async def run(root):
            ws = Workspace(root / "workspace")
            journal = StepJournal(root / "steps", workspace=ws)
            blocked = RecordedClient({"answer": [BudgetExceeded("local cap")]})
            with self.assertRaises(AwaitingBudget):
                await ModelSession(blocked, RunConfig(), "q", journal=journal).request(
                    "answer", "system", {}, lambda x: x
                )
            row = json.loads((root / "steps/000000.json").read_text())
            self.assertEqual(ws.request(row["request_id"])["status"], "prepared")
            ready = RecordedClient({"answer": [{"answer": "ok"}]})
            value = await ModelSession(
                ready, RunConfig(), "q", journal=StepJournal(root / "steps", workspace=ws)
            ).request("answer", "system", {}, lambda x: x)
            self.assertEqual(value["answer"], "ok")
            self.assertEqual(
                json.loads((root / "steps/000000.json").read_text())["request_id"],
                row["request_id"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_explicit_new_attempt_is_bound_to_same_journal_and_uncached(self):
        async def run(root):
            ws = Workspace(root / "workspace")
            unknown = RecordedClient({"answer": [OSError("lost")]})
            with self.assertRaises(UnknownRequest):
                await ModelSession(
                    unknown, RunConfig(), "q", journal=StepJournal(root / "steps", workspace=ws)
                ).request("answer", "system", {}, lambda x: x)
            first = json.loads((root / "steps/000000.json").read_text())["request_id"]
            replacement = ws.retry_request(first)
            ready = RecordedClient({"answer": [{"answer": "ok"}]})
            await ModelSession(
                ready, RunConfig(), "q", journal=StepJournal(root / "steps", workspace=ws)
            ).request("answer", "system", {}, lambda x: x)
            self.assertEqual(ws.request(replacement)["status"], "responded")
            self.assertFalse(ready.calls[0]["use_cache"])
            self.assertEqual(ws.request(first)["status"], "abandoned")

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_score_response_saved_before_evaluator_crash_is_reused(self):
        from darwinagent.contracts import EvaluationResult
        from darwinagent.engine import Pipeline
        from darwinagent.experiments.stages import selected_stage
        from darwinagent.runtime.execution import ExecutionSelection
        from tests.support.device import case, client, spec

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c, task = case(), spec(root / "assets")
            asyncio.run(
                Pipeline(client(c), root / "B0/generation").run(
                    c, task, RunConfig(protocol_attempts=1), execution=ExecutionSelection()
                )
            )
            crash = [True]

            class Evaluator:
                def __init__(self, connection):
                    self.connection = connection

                async def evaluate(self, result, questions=None):
                    await self.connection.chat(
                        role="judicator",
                        messages=[{"role": "user", "content": "score actual answer"}],
                        namespace="judge",
                    )
                    if crash[0]:
                        raise OSError("crashed after saved judge response")
                    return EvaluationResult({"score": 1}, 1, 1, 0, 0)

            def factory(connection, path):
                return Evaluator(connection)

            factory.criterion_id = "fixture-criterion-v1"
            transport = RecordedClient({"judicator": [{"score": 1}]})
            transport.aclose = AsyncMock()
            transport.ledger_summary = lambda: {"total_calls": len(transport.calls)}

            async def run():
                return await selected_stage(
                    "B0",
                    (c,),
                    task,
                    config=RunConfig(),
                    root=root,
                    client_factory=lambda name: transport,
                    evaluator_factory=factory,
                    execution=ExecutionSelection(stages=("score",)),
                )

            with self.assertRaises(OSError):
                asyncio.run(run())
            crash[0] = False
            asyncio.run(run())
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(list((root / "B0/evaluation/by-answer").glob("*.json")))

    def test_prompt_intervention_retains_completed_response_and_revises_unsent_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "workspace")
            journal = StepJournal(root / "completed", workspace=ws)
            path, _ = journal.reserve("model", {"role": "answer", "messages": ["old prompt"]})
            journal.submitted(path)
            journal.respond(path, {"content": "saved old response"})
            _, response = StepJournal(root / "completed", workspace=ws).reserve(
                "model", {"role": "answer", "messages": ["new prompt"]}
            )
            self.assertEqual(response["content"], "saved old response")
            self.assertEqual(json.loads(path.read_text())["input"]["messages"], ["old prompt"])
            prepared = StepJournal(root / "prepared", workspace=ws)
            path, _ = prepared.reserve("model", {"role": "answer", "messages": ["old prompt"]})
            first = json.loads(path.read_text())["request_id"]
            StepJournal(root / "prepared", workspace=ws).reserve(
                "model", {"role": "answer", "messages": ["new prompt"]}
            )
            latest = json.loads(path.read_text())
            self.assertEqual(ws.request(first)["status"], "abandoned")
            self.assertEqual(
                ws.read_json(ws.request(latest["request_id"])["payload_ref"])["input"]["messages"],
                ["new prompt"],
            )
            self.assertFalse(
                ws.events(kind="request_input_revised")[0]["payload"]["possible_duplicate_cost"]
            )

    def test_unknown_question_does_not_cancel_inflight_sibling_response(self):
        from dataclasses import replace

        from darwinagent.engine import Pipeline
        from darwinagent.runtime.execution import ExecutionSelection
        from tests.support.device import case, extraction, review, spec

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = case()
            c = replace(
                c,
                questions=(
                    c.questions[0],
                    replace(c.questions[0], id="q2", text="第二题：谁维护了设备？"),
                ),
            )

            class Transport:
                async def chat(self, **request):
                    payload = json.loads(request["messages"][-1]["content"])
                    role = request["role"]
                    if role == "extraction":
                        value = extraction(c)
                    elif role == "tools":
                        value = (
                            {"action": "ready"}
                            if payload.get("previous_results")
                            else {
                                "action": "call",
                                "asset_id": "device_lookup",
                                "parameters": {"serial": "D-17"},
                            }
                        )
                    elif role == "answer":
                        if "第二题" not in payload["question"]:
                            raise OSError("first request response unknown")
                        await asyncio.sleep(0.03)
                        value = {
                            "status": "answered",
                            "answer": "林于2026-09-01维护。",
                            "node_ids": ["n000000"],
                        }
                    else:
                        value = review()
                    return SimpleNamespace(content=json.dumps(value, ensure_ascii=False))

            with self.assertRaises(UnknownRequest):
                asyncio.run(
                    Pipeline(Transport(), root / "generation").run(
                        c,
                        spec(root / "assets"),
                        RunConfig(concurrency=2, protocol_attempts=1),
                        execution=ExecutionSelection(),
                    )
                )
            checkpoints = list(
                (root / "generation" / c.id / "branches/main/answers").glob("*.json")
            )
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(json.loads(checkpoints[0].read_text())["result"]["question_id"], "q2")

    def test_active_checkpoint_survives_crash_without_charging_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            clock = [0]
            interval = ActiveBudget(path, 100, clock=lambda: clock[0])
            interval.__enter__()
            clock[0] = 25
            interval.checkpoint()
            # Stop the test daemon without normal context exit, representing a killed worker.
            interval._stop.set()
            interval._heartbeat.join()
            clock[0] = 10000
            restored = ActiveBudget(path, 100, clock=lambda: clock[0])
            with restored:
                self.assertEqual(restored.remaining, 75)
                clock[0] += 5
            self.assertEqual(restored.spent, 30)
