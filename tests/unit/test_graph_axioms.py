import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oak.agents import ExtractionAgent
from oak.config import RunConfig
from oak.kg.assembler import GraphAssembler
from oak.kernel.assets import KernelAssets
from oak.kernel.execution import KernelRuntime
from oak.kernel.validation import validate_graph
from tests.fixtures import spec, case, client


class GraphAxiomAcceptance(unittest.TestCase):
    def test_cardinality_instance_constraint_is_mandatory_and_keeps_raw(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);c=case();s=spec(root/'assets')
            assets=[replace(a,content=a.content.replace(
                'axioms: []','axioms:\n  - {kind: cardinality, relation: evidence, class: AtomicFact, min: 2}'))
                if a.kind=='S' else a for a in s.bundle.assets.assets]
            runtime=KernelRuntime(KernelAssets(tuple(assets)).export(root/'axioms'),RunConfig())
            memory=asyncio.run(ExtractionAgent(runtime,client(c),RunConfig(),'test').extract(c.corpus))
            graph=GraphAssembler.build(memory,s,runtime.schema)
            with self.assertRaises(ValueError) as caught:
                validate_graph(graph,runtime.schema)
            self.assertIn('Cardinality',str(caught.exception))
            self.assertTrue(memory.raw_outputs)

    def test_static_disjoint_subclasses_rejected(self):
        from oak.schema.model import Schema
        from oak.schema.owlcheck import static_checks
        schema=Schema.from_yaml('''entity_types:
  A: {primary_key: [id], attributes: [{name: id, dtype: string}]}
  B: {primary_key: [id], attributes: [{name: id, dtype: string}]}
relation_types: {}
axioms:
  - {kind: subclass, sub: A, sup: B}
  - {kind: disjoint, classes: [A,B]}
''')
        self.assertTrue(static_checks(schema))
