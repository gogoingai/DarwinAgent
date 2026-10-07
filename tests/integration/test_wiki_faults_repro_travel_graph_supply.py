"""Offline regression scenarios for travel graph supply."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.kernel.revision import AssetRevisionService, training_id
from tests.support.travel_faults import (
    COMPLETE_C,
    FIXTURES,
    base_bundle,
    candidate_bundle,
    travel_case,
    travel_graph,
)


class DynamicTrialGraphTests(unittest.TestCase):
    """图供给身份纪律：未触碰 S/P.extract 复用已采纳真图；触碰必须重抽——
    拿旧图证明新候选安全=身份失配。试验图缓存跨候选复用（确定性产物，非请求重放）。"""

    def _runner(self, root, case):
        async def _close():
            pass

        return ExperimentRunner(
            type("Adapter", (), {"generation_input": lambda _, ident: case})(),
            lambda transport, path: None,
            None,
            RunConfig(protocol_attempts=1),
            AdoptionPolicy("final", ()),
            root,
            dynamic_trial=True,
            client_factory=lambda stage: type("Client", (), {"aclose": staticmethod(_close)})(),
        )

    def _stage_b0(self, root, base):
        generation = root / "B0" / "generation" / "train:0"
        generation.mkdir(parents=True, exist_ok=True)
        (generation / "graph.json").write_text((FIXTURES / "graph.json").read_text())
        from darwinagent.runtime.artifacts import digest

        payload = json.loads((generation / "graph.json").read_text())
        (generation / "graph.complete.json").write_text(json.dumps({"digest": digest(payload)}))
        (root / "B0" / "stage.json").write_text(
            json.dumps({"stage": "B0", "asset_version": base.version})
        )

    def test_c_only_patch_reuses_adopted_graph_s_patch_forces_rebuild(self):
        from unittest import mock

        case = travel_case()
        base = base_bundle()
        schema_asset = next(a for a in base.assets.assets if a.kind == "S")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        self._stage_b0(root, base)

        class CountingAgent:
            calls = 0

            def __init__(self, *args):
                pass

            async def extract_entities(self, corpus):
                CountingAgent.calls += 1
                return travel_graph(travel_case())

        runner = self._runner(root, case)
        with mock.patch("darwinagent.agents.ExtractionAgent", CountingAgent):
            c_only = candidate_bundle(base, {"c_answer_shape": COMPLETE_C}, str(root / "c1"))
            asyncio.run(runner._dynamic_trial_graphs(c_only, [case]))
            self.assertEqual(CountingAgent.calls, 0, "未触碰 S/P.extract 必须复用已采纳图")

            tampered = replace(
                schema_asset, content=schema_asset.content + "\n# candidate schema edit\n"
            )
            from darwinagent.kernel.revision import AssetPatch

            s_patch = AssetRevisionService().propose(
                base,
                [
                    AssetPatch(
                        tampered, schema_asset.fingerprint, "S 扩展", (training_id("train:0", "0"),)
                    )
                ],
                root / "c2",
                (training_id("train:0", "0"),),
                (),
                ("S",),
                (),
            )
            asyncio.run(runner._dynamic_trial_graphs(s_patch, [case]))
            self.assertEqual(CountingAgent.calls, 1, "触碰 S 必须用候选资产重抽真图")

    def test_extract_cache_reused_across_candidates_sharing_extraction_face(self):
        from unittest import mock

        case = travel_case()
        base = base_bundle()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)

        class CountingAgent:
            calls = 0

            def __init__(self, *args):
                pass

            async def extract_entities(self, corpus):
                CountingAgent.calls += 1
                return travel_graph(travel_case())

        runner = self._runner(root, case)
        two = [
            candidate_bundle(base, {"c_answer_shape": COMPLETE_C}, str(root / f"c{i}"))
            for i in range(2)
        ]
        with mock.patch("darwinagent.agents.ExtractionAgent", CountingAgent):
            for bundle in two:
                asyncio.run(runner._dynamic_trial_graphs(bundle, [case]))
        self.assertEqual(CountingAgent.calls, 1, "同 (case,S,P.extract,config,transport) 只抽一次")
