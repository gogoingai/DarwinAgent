"""Graph evidence reaches the training Wiki and proposer without live model calls."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from darwinagent.config import RunConfig
from darwinagent.experiments.proposal import ProposalGenerator
from darwinagent.experiments.wiki import WikiMaintainer
from darwinagent.kernel.registration import load_assets
from darwinagent.runtime.artifacts import atomic_json
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK, case


class GraphWikiTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_wiki_context_retains_asset_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = WikiMaintainer(tmp, "recorded", lambda _: LedgerRecordedClient({}), RunConfig())
            signal = {
                "asset_kinds": ["S", "P"],
                "observation": "物品声明没有物化",
                "case_id": "c",
                "question_id": "q",
                "confidence": "hypothesis",
                "next_check": "查询事实和图原件",
                "evidence": {"graph_digest": "g"},
            }
            await wiki.record(
                "B0",
                "formal",
                {"asset_change_signals": [signal], "large_context": "x" * 20000},
            )
            facts = wiki.context(4000)["entries"][0]["facts"]
            self.assertTrue(facts["view_truncated"])
            self.assertEqual(facts["asset_change_signals"][0]["asset_kinds"], ["S", "P"])
            self.assertEqual(facts["asset_change_signals"][0]["observation"], signal["observation"])
            self.assertTrue(facts["evidence_refs"])

    async def test_external_identity_uses_prepared_llm_graph(self):
        import networkx as nx

        from darwinagent.config import Config
        from darwinagent.contracts import GraphResult
        from datasets.locomo.scripts.external_test import experiment_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = load_assets(TASK).export(root / "bundle")
            g = nx.MultiDiGraph()
            graph = GraphResult(g, {})
            builder = SimpleNamespace(
                identity=lambda: {"graph_mode": "llm", "graph_builder": "recorded"}, build=object()
            )
            result = experiment_identity(
                base,
                RunConfig(),
                Config(),
                ["conv-26"],
                snap_root=Path("tests/fixtures/locomo_snapshot"),
                graph_builder=builder,
                prepared_graphs={"conv-26": graph},
            )
            self.assertEqual(result["graph_mode"], "llm")
            self.assertIn("conv-26", result["rebuilt_graphs"])

    async def test_graph_originals_and_version_deltas_are_queryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "graph.json"
            atomic_json(graph, {"nodes": [{"id": "bike", "etype": "物品"}], "edges": []})
            wiki = WikiMaintainer(root, "recorded", lambda _: LedgerRecordedClient({}), RunConfig())
            first = {
                "case_id": "c",
                "graph_digest": "g1",
                "nodes": 1,
                "edges": 0,
                "artifacts": {"graph": str(graph)},
            }
            await wiki.record("B0", "formal", {"graphs": [first]})
            second = {"case_id": "c", "graph_digest": "g2", "nodes": 2, "edges": 1}
            await wiki.record("R1", "formal", {"graphs": [second]})
            entry = next(e for e in wiki._wiki()["entries"] if e["stage"] == "R1")
            self.assertEqual(entry["facts"]["graphs"][0]["delta"], {"nodes": 1, "edges": 1})
            from darwinagent.experiments.wiki_service import WikiQuery

            result = await wiki.service.query(
                WikiQuery("物品", scope={"case_ids": ["c"]}, view="raw")
            )
            self.assertIn("bike", json.dumps(result.to_dict(), ensure_ascii=False))

    async def test_proposer_receives_asset_signals_and_diagnosis_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = load_assets(TASK).export(root / "bundle")
            client = LedgerRecordedClient(
                {"proposal": [{"action": "no_change", "reason": "先查询图原件", "unresolved": []}]}
            )
            context = {
                "entries": [
                    {
                        "facts": {
                            "asset_change_signals": [
                                {"asset_kinds": ["S"], "observation": "缺物品结构"}
                            ]
                        }
                    }
                ]
            }
            await ProposalGenerator().propose(
                base,
                (case(),),
                None,
                client,
                RunConfig(),
                root / "proposal",
                allowed_kinds=("S", "F", "C", "P"),
                wiki_context=context,
            )
            payload = json.loads(client.calls[0]["messages"][1]["content"])
            self.assertEqual(payload["asset_change_signals"][0]["asset_kinds"], ["S"])
            self.assertIn("先定位需要修改的资产", client.calls[0]["messages"][0]["content"])

    async def test_g1_assembly_uses_llm_builder_without_loading_frozen_graph(self):
        from datasets.locomo.llm_graph import LLMSnapshotGraphBuilder
        from datasets.locomo.run import main

        with tempfile.TemporaryDirectory() as tmp:
            captured = {}

            class Runner:
                def __init__(self, *args, **kwargs):
                    captured.update(kwargs)

                async def run(self, *args, **kwargs):
                    captured.update(kwargs)
                    return {"status": "recorded"}

            from tests.support.locomo import fixture_dataset

            data_dir = fixture_dataset(Path(tmp) / "input")
            args = SimpleNamespace(
                output=tmp,
                data_dir=str(data_dir),
                memory_root="tests/fixtures/locomo_snapshot",
                arm="g1",
                optimization_mode=None,
                train_only=True,
                strict_comparison=False,
                vector_k=None,
                rounds=0,
                train_questions=None,
                train_question_ids=None,
                cases="conv-26",
                scope="sfcp",
                resume=False,
                preview=False,
                stop=False,
            )
            with (
                mock.patch("datasets.locomo.run.ExperimentRunner", Runner),
                mock.patch("datasets.locomo.run.connection", return_value=object()),
                mock.patch(
                    "datasets.locomo.run.bootstrap_trial_graph",
                    side_effect=AssertionError("old graph used"),
                ),
            ):
                await main(args)
            self.assertIsInstance(captured["graph_builder"], LLMSnapshotGraphBuilder)
            self.assertEqual(captured["optimization_mode"], "wiki")
            self.assertEqual(captured["scope"], ("S", "F", "C", "P"))
