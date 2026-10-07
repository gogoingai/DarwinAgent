"""Offline regression scenarios for anchored pipeline."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.revision import (
    AssetPatch,
    AssetRevisionService,
    training_id,
)
from tests.support.anchored import anchored_bundle
from tests.support.facts import SEED, block


class AnchoredPipelineChecksTaskC(unittest.TestCase):
    """Fresh assembly must run the unified validation including task graph C (P1-2)."""

    def bundle(self, root, graph_check_ok=True):
        return anchored_bundle(root, graph_check_ok=graph_check_ok)

    def case(self):
        b1 = block("1", "我下周修打印机。")
        return b1

    def replies(self, b1):
        from tests.support.device import review

        return {
            "extraction": [
                {
                    "facts": [
                        {
                            "text": "甲计划下周维修打印机",
                            "subject": {"class": "person", "name": "甲"},
                            "predicate": "维修",
                            "object": {"entity": {"class": "object", "name": "打印机"}},
                            "polarity": "positive",
                            "modality": "plan",
                            "time": {
                                "raw": "下周",
                                "precision": "day",
                                "start": "",
                                "end": "",
                                "relative": True,
                            },
                            "evidence": [{"source_id": "m0", "quote": "我下周修打印机"}],
                        }
                    ]
                }
            ],
            "tools": [
                {"action": "call", "asset_id": "f_find", "parameters": {"predicate": "维修"}},
                {"action": "ready"},
            ],
            "answer": [
                {"status": "answered", "answer": "甲计划下周修打印机。", "node_ids": ["n000000"]}
            ],
            "review": [review()],
        }

    def run_pipeline(self, graph_check_ok):
        from darwinagent.contracts import CaseInput, QuestionInput
        from darwinagent.engine import Pipeline
        from darwinagent.kernel import TaskSpec
        from darwinagent.llm.recorded import RecordedClient

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bundle = self.bundle(root, graph_check_ok)
        spec = TaskSpec.load(SEED.parents[2] / "task.yaml", bundle).with_bundle(bundle)
        b1 = self.case()
        case = CaseInput("c1", (b1,), (QuestionInput("q1", "甲计划做什么？"),))
        transport = RecordedClient(self.replies(b1))
        result = asyncio.run(
            Pipeline(transport, root / "generation").run(case, spec, RunConfig(protocol_attempts=1))
        )
        return root / "generation" / "c1", result

    def test_passing_check_answers(self):
        _, result = self.run_pipeline(True)
        self.assertEqual(result.answers[0].status, "answered")
        self.assertEqual(result.memory_count, 1)

    def test_rejecting_graph_check_fails_fresh_run_too(self):
        case_dir, result = self.run_pipeline(False)
        self.assertEqual(result.answers[0].status, "execution_error")
        self.assertIn("总是拒绝", result.answers[0].error)
        self.assertTrue((case_dir / "memory.complete.json").exists())


class AnchoringModeFrozen(unittest.TestCase):
    def test_s_patch_cannot_flip_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            assets = [
                Asset(
                    "schema",
                    "S",
                    SEED.read_text(),
                    {"type": "any"},
                    {"type": "any"},
                    (),
                    description="seed",
                ),
                Asset(
                    "f1",
                    "F",
                    "def run(params):\n return []",
                    {"type": "object"},
                    {"type": "array"},
                    ["schema"],
                    description="f",
                    trial_inputs=({},),
                ),
                Asset(
                    "c1",
                    "C",
                    'def check(candidate):\n return {"ok": True, "issues": []}',
                    stage="graph",
                    schema_dependencies=["schema"],
                    description="c",
                ),
                *[
                    Asset(f"p_{r}", "P", t, role=r, schema_dependencies=["schema"], description=r)
                    for r, t in [
                        ("extract", "e ${schema}"),
                        ("tools", "t ${schema} ${tools}"),
                        ("answer", "a ${schema}"),
                        ("review", "r ${schema}"),
                    ]
                ],
            ]
            base = KernelAssets(tuple(assets), {"kind": "test"}).export(root / "base")
            s = base.get("schema")
            dropped = replace(s, content=s.content.replace("  anchoring: fact-centric-v1\n", ""))
            ev = (training_id("conv-t", "q1"),)
            with self.assertRaises(ValueError) as caught:
                AssetRevisionService().propose(
                    base,
                    [AssetPatch(dropped, s.fingerprint, "去掉锚定", ev)],
                    root / "candidate",
                    list(ev),
                )
            self.assertIn("冻结", str(caught.exception))
