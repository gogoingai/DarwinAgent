from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import networkx as nx

from oak.config import Config
from oak.kg.graph import EntityCandidate, RelationCandidate, GraphValidationError, build_graph, node_id
from oak.kernel import Harness, KernelAssets, KernelBundle
from oak.kernel.checks import CHECKS, GraphAccessors
from oak.kernel.revision import Patch, propose
from oak.llm.client import BudgetExceeded, LLMClient
from oak.operators import library
from oak.operators.sandbox import _run_with_timeout
from oak.runtime import atomic_json
from oak.schema.model import Schema
from oak.schema.owlcheck import check_schema
from oak_domains.conversation_memory.harness import parse_structured

YAML = """entity_types:
  Person:
    primary_key: [name]
    attributes: [{name: name, dtype: string}, {name: age, dtype: int}]
  Machine:
    primary_key: [serial]
    attributes: [{name: serial, dtype: string}]
relation_types:
  owns: {domain: Person, range: Machine, functional: true}
"""


class GraphContracts(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_yaml(YAML)

    def test_undeclared_and_incomplete_entities(self):
        for entity in [EntityCandidate("Alien", {"name": "A"}, {}, "x"),
                       EntityCandidate("Person", {}, {}, "x"),
                       EntityCandidate("Person", {"name": ""}, {}, "x"),
                       EntityCandidate("Person", {"name": "A"}, {"unregistered": 1}, "x"),
                       EntityCandidate("Person", {"name": "A"}, {"age": "old"}, "x")]:
            with self.subTest(entity=entity), self.assertRaises(GraphValidationError):
                build_graph([entity], [], self.schema)

    def test_relations_cannot_inject_bad_types_or_signatures(self):
        for relation in [RelationCandidate("unknown", ("Person", {"name": "A"}), ("Machine", {"serial": "B"})),
                         RelationCandidate("owns", ("Alien", {"name": "A"}), ("Machine", {"serial": "B"})),
                         RelationCandidate("owns", ("Machine", {"serial": "A"}), ("Person", {"name": "B"}))]:
            with self.subTest(relation=relation), self.assertRaises(GraphValidationError):
                build_graph([], [relation], self.schema)

    def test_functional_conflict(self):
        relations = [RelationCandidate("owns", ("Person", {"name": "A"}), ("Machine", {"serial": s})) for s in ("1", "2")]
        with self.assertRaises(GraphValidationError):
            build_graph([], relations, self.schema)

    def test_isolation_is_explicit_and_reported(self):
        graph = build_graph([EntityCandidate("Alien", {}, {}, "x")], [], self.schema, on_invalid="isolate")
        self.assertEqual(len(graph), 0)
        self.assertEqual(len(graph.graph["validation_errors"]), 1)

    def test_identity_boundaries_and_types(self):
        self.assertNotEqual(node_id("T", {"a": "x|b=y", "c": "z"}), node_id("T", {"a": "x", "b": "y|c=z"}))
        self.assertNotEqual(node_id("T", {"key": 1}), node_id("T", {"key": "1"}))
        self.assertNotEqual(node_id("T", {"key": " A"}), node_id("T", {"key": "A"}))
        self.assertEqual(node_id("T", {"a": "字", "b": 2}), node_id("T", {"b": 2, "a": "字"}))

    def test_legal_merge_and_types(self):
        graph = build_graph([EntityCandidate("Person", {"name": "A"}, {"age": "2"}, "one"),
                             EntityCandidate("Person", {"name": "A"}, {}, "two")], [], self.schema)
        row = graph.nodes[node_id("Person", {"name": "A"})]
        self.assertEqual(row["age"], 2)
        self.assertEqual(row["__sources__"], ["one", "two"])


class RuntimeContracts(unittest.TestCase):
    def test_timeout_terminates_worker(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            _run_with_timeout(lambda: time.sleep(5), {}, .03, nx.MultiDiGraph())
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(_run_with_timeout(lambda: 7, {}, 1, nx.MultiDiGraph()), 7)

    def test_unverified_formal_check_is_not_pass(self):
        with patch("oak.schema.owlcheck.hermit_checks", return_value=([], False)):
            findings, verified = check_schema(Schema.from_yaml(YAML))
        self.assertFalse(verified)
        self.assertTrue(any(f.check == "unverified" for f in findings))

    def test_slot_extractor_and_graph_are_context_local(self):
        async def one(value):
            graph = nx.MultiDiGraph(); graph.add_node(value)
            library.set_graph(graph)
            library.set_slot_extractor(lambda *_: {"value": value})
            await asyncio.sleep(0)
            return list(library.get_graph()), library.extract_runtime_slots("q", [])
        async def run():
            return await asyncio.gather(one("A"), one("B"))
        self.assertEqual(asyncio.run(run()), [(["A"], {"value": "A"}), (["B"], {"value": "B"})])

    def test_budget_atomic_and_restart_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(api_key="fake", work_dir=Path(td), namespace_limits={"application": 2})
            async def run():
                async with LLMClient(cfg) as client:
                    self.assertIs(client._client, client._client_fast)
                    key = client._cache_key(cfg.model_strong, [], .3, 4096, False,
                        endpoint=cfg.api_base_url, thinking_off=False)
                    for namespace in ["application_a", "application_b", "application_c"]:
                        atomic_json(cfg.cache_dir / namespace / f"{key}.json", {"content": "cached", "usage": {}})
                    results = await asyncio.gather(*(client.chat(role="schema", messages=[], namespace=ns)
                        for ns in ["application_a", "application_b", "application_c"]), return_exceptions=True)
                    self.assertEqual(sum(isinstance(r, BudgetExceeded) for r in results), 1)
                async with LLMClient(cfg) as client:
                    with self.assertRaises(BudgetExceeded):
                        await client.chat(role="schema", messages=[], namespace="application_d")
            asyncio.run(run())

    def test_cache_distinguishes_endpoint_and_effective_policy(self):
        base = LLMClient._cache_key("same", [], 0, 20, False, endpoint="one")
        self.assertNotEqual(base, LLMClient._cache_key("same", [], 0, 20, False, endpoint="two"))
        self.assertNotEqual(base, LLMClient._cache_key("same", [], 0, 20, False, endpoint="one", thinking_off=True))

    def test_multiple_clients_cannot_each_spend_last_scope_credit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(api_key="fake", work_dir=Path(td), namespace_limits={"app": 1, "app_query": 2})
            async def run():
                async with LLMClient(cfg) as first, LLMClient(cfg) as second:
                    key = first._cache_key(cfg.model_strong, [], .3, 4096, False,
                        endpoint=cfg.api_base_url, thinking_off=False)
                    for ns in ("app_query_a", "app_query_b"):
                        atomic_json(cfg.cache_dir / ns / f"{key}.json", {"content": "cached"})
                    await first.chat(role="schema", messages=[], namespace="app_query_a")
                    with self.assertRaises(BudgetExceeded):
                        await second.chat(role="schema", messages=[], namespace="app_query_b")
            asyncio.run(run())


class AssetContracts(unittest.TestCase):
    def test_bundle_relocation_tamper_and_harness_revision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "schema.yaml").write_text(YAML)
            atomic_json(root / "harness.json", Harness().to_dict())
            assets = KernelAssets(schema_path=root / "schema.yaml", harness_path=root / "harness.json")
            bundle = assets.export(root / "export")
            self.assertEqual(bundle.harness(), Harness())
            self.assertFalse(bundle.schema().validate())
            changed = Harness(max_steps=3)
            candidate = propose(bundle, Patch("harness", "harness", bundle.manifest["digest"],
                json.dumps(changed.to_dict()), "Reduce excessive steps", ("train:1",)), root / "candidate")
            self.assertEqual(candidate.harness().max_steps, 3)
            self.assertEqual(bundle.harness().max_steps, 10)
            with self.assertRaises(ValueError):
                propose(bundle, Patch("evaluation", "judge", bundle.manifest["digest"], "", "reason", ("train:1",)), root / "illegal")
            bundle.path("schema", "schema").write_text("tampered")
            with self.assertRaises(ValueError):
                KernelBundle.load(root / "export")

    def test_missing_asset_is_error(self):
        with self.assertRaises(FileNotFoundError):
            KernelAssets(schema_path=Path("/nonexistent/oak-schema.yaml")).manifest()

    def test_structured_answer_cannot_invent_evidence(self):
        obj = {"requirements": ["all elements"], "claims": [{"text": "A", "evidence": ["missing"]}],
               "answer": "A", "evidence": ["missing"], "refused": False}
        with self.assertRaises(ValueError):
            parse_structured(json.dumps(obj), {"known": "A"}, "unknown")
        obj.update(answer="unknown", evidence=[], refused=True, claims=[])
        self.assertEqual(parse_structured(json.dumps(obj), {}, "unknown")[:2], ("unknown", []))


if __name__ == "__main__":
    unittest.main()
