"""Shared declarative schemas for graph and formal-validation contracts."""

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
