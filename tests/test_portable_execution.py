import json
import tempfile
import unittest
from pathlib import Path

import networkx as nx

from oak.contracts import AnswerResult
from oak.kernel import Harness, KernelAssets
from oak.kernel.execution import KernelRuntime
from oak.kg.graph import EntityCandidate, RelationCandidate, build_graph, node_id
from oak.operators import library
from oak.schema.model import Schema
from oak.runtime import atomic_json

SCHEMA = """entity_types:
  Device:
    primary_key: [serial]
    attributes: [{name: serial, dtype: string}]
  Technician:
    primary_key: [name]
    attributes: [{name: name, dtype: string}]
relation_types:
  maintained_by: {domain: Device, range: Technician}
"""


class PortableExecution(unittest.TestCase):
    def test_related_primary_keys_are_visible_to_operators(self):
        schema = Schema.from_yaml(SCHEMA)
        graph = build_graph([EntityCandidate("Device", {"serial": "A"}, {}, "1"),
                             EntityCandidate("Technician", {"name": "林"}, {}, "2")],
            [RelationCandidate("maintained_by", ("Device", {"serial": "A"}), ("Technician", {"name": "林"}))], schema)
        library.set_graph(graph)
        self.assertEqual(library.traverse_relations(node_id("Device", {"serial": "A"}), "maintained_by")[0]["name"], "林")
        devices = library.lookup_entities("Device")
        self.assertEqual(library.filter_relation_connected(devices, "maintained_by", "Technician", {"name": "林"}), devices)
        self.assertEqual(library.filter_relation_connected(devices, "maintained_by", "Technician", {"name": "王"}), [])

    def test_relocated_function_executes_and_tamper_blocks_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "schema.yaml").write_text(SCHEMA)
            (root / "function.py").write_text('def count_devices():\n    return len(lookup_entities("Device"))\n')
            atomic_json(root / "harness.json", Harness().to_dict())
            bundle = KernelAssets(schema_path=root / "schema.yaml", harness_path=root / "harness.json",
                                  function_paths=[root / "function.py"]).export(root / "export")
            runtime = KernelRuntime(bundle)
            self.assertEqual(runtime.call("0", nx.MultiDiGraph(), {}), 0)
            with self.assertRaises(ValueError):
                runtime.call("unpublished", nx.MultiDiGraph(), {})
            bundle.path("functions", "0").write_text('def count_devices():\n    return 10\n')
            with self.assertRaises(ValueError):
                runtime.call("0", nx.MultiDiGraph(), {})

    def test_execution_failure_cannot_become_refusal(self):
        with self.assertRaises(ValueError):
            AnswerResult("q", "abstained", "Unknown", error="timeout")
        with self.assertRaises(ValueError):
            AnswerResult("q", "answered", "A")
        self.assertEqual(AnswerResult("q", "execution_error", "", error="timeout").status, "execution_error")
