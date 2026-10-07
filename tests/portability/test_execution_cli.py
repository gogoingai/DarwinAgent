import argparse
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from darwinagent.cli import main
from darwinagent.runtime.execution_cli import add_execution_arguments, selection_from_args
from tests.support.device import case


class ScopedCLI(unittest.TestCase):
    def test_all_modes_share_scope_and_strict_is_opt_in(self):
        for mode in ("continue", "retry_failed", "rerun", "fork", "rerun_all"):
            parser = argparse.ArgumentParser()
            add_execution_arguments(parser)
            args = parser.parse_args(
                [
                    "--execution-mode",
                    mode,
                    "--execution-stages",
                    "answer,check",
                    "--execution-question-ids",
                    "q1,q3",
                    "--execution-branch",
                    "new",
                ]
            )
            selection = selection_from_args(args)
            self.assertEqual(mode, selection.mode)
            self.assertEqual(("answer", "check"), selection.stages)
            self.assertEqual(("q1", "q3"), selection.question_ids)
            self.assertFalse(selection.strict)
        self.assertTrue(selection_from_args(parser.parse_args(["--strict-comparison"])).strict)

    def test_demo_preview_does_not_construct_model_or_read_live_config(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            redirect_stdout(io.StringIO()) as out,
            patch("darwinagent.config.Config.from_env", side_effect=AssertionError("model config")),
        ):
            status = main(["demo", "--output", tmp, "--preview", "--execution-stages", "score"])
        self.assertEqual(0, status)
        plan = json.loads(out.getvalue())
        self.assertEqual([], plan["execute"])
        self.assertEqual("score", plan["missing"][0]["stage"])

    def test_locomo_preview_is_question_scoped_and_offline(self):
        from datasets.locomo import run

        with (
            tempfile.TemporaryDirectory() as tmp,
            redirect_stdout(io.StringIO()) as out,
            patch.object(run, "connection", side_effect=AssertionError("model connection")),
        ):
            asyncio.run(
                run.main(
                    SimpleNamespace(
                        output=tmp,
                        preview=True,
                        case="conv-26",
                        execution_question_ids="15,23",
                        execution_stages="answer",
                        execution_mode="continue",
                        execution_branch="main",
                        strict_comparison=False,
                    )
                )
            )
        plan = json.loads(out.getvalue())
        self.assertEqual({"15", "23"}, {row["question_id"] for row in plan["execute"]})

    def test_travel_preview_avoids_assets_and_model_connection(self):
        from datasets.travelplanner import run

        adapter = SimpleNamespace(generation_input=lambda _id: case())
        with (
            tempfile.TemporaryDirectory() as tmp,
            redirect_stdout(io.StringIO()) as out,
            patch.object(run, "TravelPlannerAdapter", return_value=adapter),
            patch.object(run, "load_connection", side_effect=AssertionError("model connection")),
            patch.object(run, "load_assets", side_effect=AssertionError("asset mutation")),
        ):
            asyncio.run(
                run.main(
                    SimpleNamespace(
                        output=tmp, preview=True, split="train", index=0, execution_stages="score"
                    )
                )
            )
        plan = json.loads(out.getvalue())
        self.assertEqual([], plan["execute"])
        self.assertTrue(plan["missing"])
