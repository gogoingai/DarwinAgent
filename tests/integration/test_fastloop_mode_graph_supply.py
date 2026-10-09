"""Offline regression scenarios for graph supply."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.device import TASK
from tests.support.recorded_fastloop import FastLoopExperiment


class RebuildSupplyCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_cache_reuses_same_schema_and_rebuilds_on_change(self):
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
                await runner._rebuild_graph_cached(bundle, case)
                await runner._rebuild_graph_cached(bundle, case)
                self.assertEqual(len(calls), 1)  # 同 S → 复用
                fake_schema.to_yaml.return_value = "schema-v2"
                await runner._rebuild_graph_cached(bundle, case)
                self.assertEqual(len(calls), 2)  # S 变 → 重建

    async def test_async_builder_receives_runtime_and_rebuilds_on_extract_prompt(self):
        from dataclasses import replace

        from darwinagent.kernel.assets import KernelAssets
        from darwinagent.kernel.registration import load_assets
        from tests.support.clients import LedgerRecordedClient

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            class Builder:
                uses_extract_prompt = True

                def identity(self):
                    return {"graph_mode": "llm", "graph_builder": "recorded"}

                async def build(self, snapshot, runtime, corpus, client, config, **kwargs):
                    calls.append(runtime.prompt("extract"))
                    return object()

            runner = FastLoopExperiment(root, graph_builder=Builder())
            runner._client = lambda stage: LedgerRecordedClient({})
            runner.snapshot_root = Path("tests/fixtures/locomo_snapshot")
            from types import SimpleNamespace

            case = SimpleNamespace(id="conv-26", corpus=())
            assets = load_assets(TASK)
            bundle = assets.export(root / "first")
            first = await runner._rebuild_graph_cached(bundle, case)
            self.assertIs(first, await runner._rebuild_graph_cached(bundle, case))
            updated = KernelAssets(
                tuple(
                    replace(a, content=a.content + "\nUse source evidence.")
                    if a.kind == "P" and a.role == "extract"
                    else a
                    for a in assets.assets
                )
            ).export(root / "second")
            await runner._rebuild_graph_cached(updated, case)
            self.assertEqual(len(calls), 2)
