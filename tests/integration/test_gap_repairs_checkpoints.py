"""Offline regression scenarios for checkpoints."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.kernel import KernelBundle
from darwinagent.kernel.registration import load_assets
from tests.support.device import TASK


class SeedAssetsAnchor(unittest.TestCase):
    """③：锁定 bundle 锚定 B0——不冷启动、seed 入身份、门槛照跑。"""

    def test_seed_skips_bootstrap_and_records_identity(self):
        from tests.support.recorded_fastloop import FastLoopExperiment, _run

        with tempfile.TemporaryDirectory() as tmp:
            holder = Path(tmp) / "seed"
            seed_dir = load_assets(TASK).export(holder / "bundle").root
            root = Path(tempfile.mkdtemp(dir=tmp))

            class SeededRun(FastLoopExperiment):
                def _client(self, stage):
                    if stage == "B0":
                        # 种子路径没有冷启动：第一份客户端不再留给 bootstrap 回复
                        self.stage_clients[stage] += 1
                    return super()._client(stage)

            runner = SeededRun(root, seed_assets=seed_dir)
            with mock.patch(
                "darwinagent.experiments.bootstrap.AssetBootstrapper",
                side_effect=AssertionError("冷启动不应执行"),
            ):
                summary = _run(runner, rounds=0)
            self.assertEqual(summary["status"], "complete")
            self.assertTrue((root / "B0" / "assets" / "manifest.json").exists())
            seed_record = json.loads((root / "B0" / "seed.json").read_text())
            self.assertEqual(seed_record["version"], KernelBundle(seed_dir).version)
            decl = json.loads((root / "experiment.json").read_text())
            self.assertEqual(len(decl["seed_assets"]), 1)
            self.assertFalse((root / "B0" / "bootstrap-call.json").exists())
