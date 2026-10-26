import copy
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path

from darwinagent.runtime.artifacts import digest
from datasets.lift.full_trajectory import (
    build_native_judge_input,
    extract_trajectory,
    score_trajectory,
)
from tests.support.full_lift_trajectory import RecordedShared, recorded_transcript


class ProjectedJudgeInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import src.lift  # noqa: F401
        except ModuleNotFoundError as exc:
            if exc.name not in {"src", "src.lift"}:
                raise
            raise unittest.SkipTest("Optional pinned LIFT upstream is not configured") from None

    async def fixture(self, root):
        from src.postprocess import judge

        await recorded_transcript(root)
        transcript = extract_trajectory(root)
        source = str(Path(judge.__file__).resolve())
        return transcript, {source: hashlib.sha256(Path(source).read_bytes()).hexdigest()}

    async def test_opt_in_envelope_replays_and_default_stays_native(self):
        self.assertIn(
            "reference_projection", inspect.signature(build_native_judge_input).parameters
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, sources = await self.fixture(root)
            kwargs = dict(
                task_query="synthetic", trajectory_reqs="native requirement", source_hashes=sources
            )
            original = build_native_judge_input(raw, **kwargs)
            envelope = build_native_judge_input(raw, reference_projection=True, **kwargs)
            self.assertNotIn("trajectory_projection", original)
            self.assertEqual(envelope["transcript"], raw)
            self.assertEqual(envelope["messages"], original["messages"])
            shared = RecordedShared()
            first = await score_trajectory(shared, "s", root / "judge", envelope)
            second = await score_trajectory(shared, "s", root / "judge", envelope)
            self.assertEqual(first, second)
            self.assertEqual(shared.calls, 1)

    async def test_rehashed_projection_or_prompt_tamper_rejected_before_send(self):
        self.assertIn(
            "reference_projection", inspect.signature(build_native_judge_input).parameters
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, sources = await self.fixture(root)
            envelope = build_native_judge_input(
                raw,
                task_query="synthetic",
                trajectory_reqs="native requirement",
                source_hashes=sources,
                reference_projection=True,
            )
            for kind in ("manifest", "prompt"):
                bad = copy.deepcopy(envelope)
                if kind == "manifest":
                    bad["trajectory_projection"]["protocol"] = "forged"
                else:
                    bad["messages"][1]["content"] = "omit errors"
                bad["sha256"] = digest({k: v for k, v in bad.items() if k != "sha256"})
                shared = RecordedShared()
                with self.assertRaises(ValueError):
                    await score_trajectory(shared, "s", root / kind, bad)
                self.assertEqual(shared.calls, 0)
