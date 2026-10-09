import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from datasets.locomo import run
from datasets.locomo.run import main, run_arm
from tests.support.locomo import fixture_dataset


def arguments(root, **overrides):
    values = {
        "output": str(root),
        "arm": "g1",
        "scope": "sfcp",
        "vector_k": 30,
        "optimization_mode": "wiki",
        "train_only": True,
        "train_questions": 10,
        "rounds": 10,
        "stop": False,
        "strict_comparison": True,
        "cases": None,
        "resume": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WikiCliBoundary(unittest.TestCase):
    def test_rejects_wrong_arm_and_patch_scope_before_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "requires --arm g1 --scope sfcp"):
                asyncio.run(run_arm(arguments(tmp, scope="p")))
            with self.assertRaisesRegex(ValueError, "requires --arm g1"):
                with mock.patch.object(
                    run, "resolve_dataset", return_value=fixture_dataset(Path(tmp) / "input")
                ):
                    asyncio.run(main(arguments(tmp, arm=None)))

    def test_requires_passing_precheck_for_new_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("datasets.locomo.run.connection", return_value=object()),
                mock.patch("datasets.locomo.run.precheck_identity", return_value="identity"),
            ):
                with self.assertRaisesRegex(ValueError, "requires a passing precheck"):
                    asyncio.run(run_arm(arguments(tmp)))
                Path(tmp, "precheck.json").write_text('{"passed": true, "identity": "other-code"}')
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    asyncio.run(run_arm(arguments(tmp)))

    def test_daily_training_continuation_does_not_require_new_model_precheck(self):
        from unittest.mock import AsyncMock, MagicMock

        runner = MagicMock()
        runner.run = AsyncMock(return_value={"status": "complete"})
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("datasets.locomo.run.connection", return_value=object()),
            mock.patch(
                "datasets.locomo.run.precheck_identity",
                side_effect=AssertionError("probe identity"),
            ),
            mock.patch("datasets.locomo.run.memory_structure_sample", return_value={}),
            mock.patch("datasets.locomo.run.DATA_DIR", Path("explicit-input")),
            mock.patch("datasets.locomo.run.LocomoAdapter"),
            mock.patch("datasets.locomo.run.bootstrap_trial_graph", return_value=None),
            mock.patch("datasets.locomo.run.ExperimentRunner", return_value=runner),
        ):
            asyncio.run(
                run_arm(
                    arguments(
                        tmp,
                        strict_comparison=False,
                        scope="p",
                        graph_mode="frozen",
                        memory_root="tests/fixtures/locomo_snapshot",
                    )
                )
            )
        self.assertFalse(runner.run.call_args.kwargs["execution"].strict)
        self.assertEqual(("P",), runner.run.call_args.kwargs["scope"])
