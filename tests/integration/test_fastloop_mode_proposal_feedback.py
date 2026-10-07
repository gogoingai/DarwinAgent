"""Offline regression scenarios for proposal feedback."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.support.recorded_fastloop import FastLoopExperiment, _run


class PExtractMarkingTests(unittest.TestCase):
    def test_extract_patch_marked_not_effective_in_rebuild_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class ExtractPatcher(FastLoopExperiment):
                patched = False

                def _client(self, stage):
                    transport = super()._client(stage)
                    replies = getattr(transport, "replies", None)
                    if (
                        stage.startswith("R")
                        and replies
                        and "proposal" in replies
                        and not self.patched
                    ):
                        self.patched = True
                        pointer = json.loads((self.root / "published/current.json").read_text())
                        from darwinagent.kernel import KernelBundle

                        base = KernelBundle(self.root / "published" / pointer["path"])
                        asset = next(a for a in base.assets.assets if a.role == "extract")
                        updated = asset.to_dict()
                        updated["content"] += "\nReweight extraction emphasis."
                        from collections import deque

                        from darwinagent.kernel.revision import training_id

                        replies["proposal"] = deque(
                            [
                                {
                                    "patches": [
                                        {
                                            "asset": updated,
                                            "base_fingerprint": asset.fingerprint,
                                            "reason": "extract emphasis",
                                            "training_evidence": [
                                                training_id(self.case.id, self.case.questions[0].id)
                                            ],
                                        }
                                    ]
                                }
                            ]
                        )
                    return transport

            runner = ExtractPatcher(root, graph_builder=object())
            summary = _run(runner, rounds=1)
            decision = summary["rounds"][0]
            self.assertIn("p_extract_not_effective", decision, decision)
            wiki = json.loads((root / "optimization/wiki.json").read_text())
            formal = next(
                e for e in wiki["entries"] if e["stage"] == "R1" and e["kind"] == "formal"
            )
            self.assertIn("p_extract_not_effective", formal["facts"])
