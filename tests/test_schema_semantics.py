import unittest

from oak.schema.model import Schema
from oak.schema.owlcheck import _build_owl, static_checks


class SchemaSemantics(unittest.TestCase):
    def test_invalid_cardinality_does_not_reach_reasoner(self):
        from test_framework_contracts import YAML
        schema = Schema.from_yaml(YAML + '\naxioms:\n  - {kind: cardinality, relation: owns, class: Person, min: 3, max: 1}\n')
        self.assertTrue(any("min" in error for error in schema.validate()))

    def test_union_domain_and_actual_cardinality_translation(self):
        try:
            import owlready2 as owl
        except ImportError:
            self.skipTest("optional formal dependency")
        schema = Schema.from_yaml('''entity_types:
  A: {primary_key: [id], attributes: [{name: id, dtype: string}]}
  B: {primary_key: [id], attributes: [{name: id, dtype: string}]}
relation_types:
  related: {domain: [A, B], range: B}
axioms:
  - {kind: cardinality, relation: related, class: A, min: 2, max: 3}
''')
        world, _, classes, relations, _ = _build_owl(schema)
        try:
            self.assertIsInstance(relations["related"].domain[0], owl.Or)
            restrictions = [r for r in classes["A"].is_a if isinstance(r, owl.Restriction)]
            self.assertEqual({r.cardinality for r in restrictions}, {2, 3})
            self.assertTrue(all(r.value is classes["B"] for r in restrictions))
        finally:
            world.close()
