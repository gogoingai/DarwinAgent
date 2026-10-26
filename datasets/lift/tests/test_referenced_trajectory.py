"""Complete judge evidence without copying original request snapshots."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.runtime.artifacts import digest
from datasets.lift.full_trajectory import _verify, extract_trajectory
from tests.support.full_lift_trajectory import recorded_transcript


class ReferencedTrajectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_mode_keeps_all_messages_costs_and_original_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await recorded_transcript(root, macro=True)
            before = {str(p): p.read_bytes() for p in root.rglob("*.json")}
            legacy = extract_trajectory(root)
            lean = extract_trajectory(root, include_snapshots=False)
            self.assertEqual(lean["all_messages"], legacy["all_messages"])
            self.assertEqual(lean["source_hashes"], legacy["source_hashes"])
            self.assertEqual(lean["source_inventory"], legacy["source_inventory"])
            self.assertEqual(lean["leaf_calls"], legacy["leaf_calls"])
            self.assertEqual(lean["model_calls"], len(legacy["raw_requests"]))
            self.assertNotIn("raw_requests", lean)
            self.assertLess(len(json.dumps(lean)), len(json.dumps(legacy)))
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*.json")})
            _verify(lean)
            self.assertIn("raw_requests", extract_trajectory(root))

    async def test_rehashed_message_forgery_is_rejected_against_source_journals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await recorded_transcript(root)
            lean = extract_trajectory(root, include_snapshots=False)
            bad = copy.deepcopy(lean)
            bad["all_messages"][-1]["content"] = "hide known failure"
            bad["sha256"] = digest({k: v for k, v in bad.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                _verify(bad)

    async def test_native_value_projection_restores_complete_trace_and_keeps_rules(self):
        try:
            import src.lift  # noqa: F401
        except ModuleNotFoundError as exc:
            if exc.name not in {"src", "src.lift"}:
                raise
            raise unittest.SkipTest("Optional pinned LIFT upstream is not configured") from None
        import hashlib

        from src.postprocess import judge

        from datasets.lift.full_trajectory import (
            build_native_judge_input,
            verify_native_judge_projection,
        )
        from datasets.lift.trace_values import decode

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await recorded_transcript(root, macro=True)
            lean = extract_trajectory(root, include_snapshots=False)
            path = str(Path(judge.__file__).resolve())
            kwargs = dict(
                task_query="synthetic",
                trajectory_reqs="original rules",
                source_hashes={path: hashlib.sha256(Path(path).read_bytes()).hexdigest()},
            )
            plain = build_native_judge_input(lean, **kwargs)
            value = build_native_judge_input(lean, value_projection=True, **kwargs)
            self.assertEqual(value["schema"], "full-lift-judge-input-v5")
            self.assertEqual(value["messages"][0], plain["messages"][0])
            self.assertEqual(
                decode(value["value_refs"]),
                [
                    judge.format_content_blocks(m["content"])
                    for m in lean["all_messages"]
                    if m.get("content") is not None
                ],
            )
            verify_native_judge_projection(value)
            readable = build_native_judge_input(
                lean, value_projection=True, readable_value_projection=True, **kwargs
            )
            self.assertEqual(readable["schema"], "full-lift-judge-input-v6")
            self.assertEqual(readable["messages"][0], plain["messages"][0])
            self.assertEqual(readable["value_refs"], value["value_refs"])
            verify_native_judge_projection(readable)
            readable["messages"][1]["content"] += "forged extra action"
            readable["sha256"] = digest({k: v for k, v in readable.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                verify_native_judge_projection(readable)
            value["messages"][1]["content"] = "hide a failed primitive"
            value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                verify_native_judge_projection(value)
