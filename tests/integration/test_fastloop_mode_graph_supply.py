"""Offline regression scenarios for graph supply."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.device import TASK
from tests.support.recorded_fastloop import FastLoopExperiment


class RebuildSupplyCacheTests(unittest.TestCase):
    def test_rebuild_cache_reuses_same_schema_and_rebuilds_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FastLoopExperiment(Path(tmp), graph_builder=lambda *a: object())
            from darwinagent.kernel.registration import load_assets as la

            bundle = la(TASK).export(Path(tmp) / "bundle")
            calls = []

            def counting(snapshot_dir, schema, corpus=(), embedder_factory=None):
                calls.append(schema.to_yaml()[:40])
                return object()

            runner.graph_builder = counting
            from types import SimpleNamespace

            case = SimpleNamespace(id="conv-26")
            runner.snapshot_root = Path("tests/fixtures/locomo_snapshot")
            fake_schema = mock.MagicMock()
            fake_schema.to_yaml.return_value = "schema-v1"
            with mock.patch(
                "darwinagent.kernel.validation.validate_bundle", return_value=fake_schema
            ):
                runner._rebuild_graph_cached(bundle, case)
                runner._rebuild_graph_cached(bundle, case)
                self.assertEqual(len(calls), 1)  # 同 S → 复用
                fake_schema.to_yaml.return_value = "schema-v2"
                runner._rebuild_graph_cached(bundle, case)
                self.assertEqual(len(calls), 2)  # S 变 → 重建
