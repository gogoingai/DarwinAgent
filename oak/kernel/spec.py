"""Declarative task and asset contracts. No execution callbacks or capability grants."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from oak.contracts import freeze, plain

KINDS = frozenset({"S", "F", "C", "P"})
PROMPT_SLOTS = {"extract": frozenset({"schema"}), "tools": frozenset({"schema", "tools"}),
                "answer": frozenset({"schema"}), "review": frozenset({"schema"})}
RESERVED_PREFIX = "fixed."


def validate_value(value, contract, path="value"):
    """Small, closed JSON contract language; unsupported constraints are rejected."""
    if not isinstance(contract, Mapping):
        raise ValueError("Contract must be an object")
    if set(contract) - {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}:
        raise ValueError("Unsupported contract keyword")
    types = {"object": lambda x: isinstance(x, Mapping), "array": lambda x: isinstance(x, (tuple, list)),
             "string": lambda x: type(x) is str, "integer": lambda x: type(x) is int,
             "number": lambda x: type(x) in (int, float), "boolean": lambda x: type(x) is bool,
             "null": lambda x: x is None, "any": lambda x: True}
    kind = contract.get("type")
    if kind not in types or not types[kind](value):
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in contract and value not in contract["enum"]:
        raise ValueError(f"{path}: not in enum")
    if kind == "object":
        props = contract.get("properties", {})
        if set(contract.get("required", [])) - set(value):
            raise ValueError(f"{path}: missing required fields")
        if contract.get("additionalProperties", False) is False and set(value) - set(props):
            raise ValueError(f"{path}: undeclared fields {set(value) - set(props)}")
        for key in value.keys() & props.keys():
            validate_value(value[key], props[key], f"{path}.{key}")
    if kind == "array" and "items" in contract:
        for n, item in enumerate(value):
            validate_value(item, contract["items"], f"{path}[{n}]")


@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    source_kinds: tuple[str, ...]
    metadata_keys: tuple[str, ...] = ()
    parameter_contract: Mapping = field(default_factory=lambda: {"type": "object", "properties": {}})
    answer_format: str = "text"
    answer_contract: Mapping = field(default_factory=lambda: {"type": "string"})
    bundle: object | None = None

    def __post_init__(self):
        if not self.name or not self.description or not self.source_kinds:
            raise ValueError("Task requires a name, description and declared source kinds")
        if self.answer_format not in {"text", "json"}:
            raise ValueError("Unknown answer format")
        object.__setattr__(self, "parameter_contract", freeze(self.parameter_contract))
        object.__setattr__(self, "answer_contract", freeze(self.answer_contract))
        if any(callable(x) for x in self.__dict__.values()):
            raise ValueError("Task cannot contain execution callbacks")

    def declaration(self):
        return {"name": self.name, "description": self.description, "source_kinds": list(self.source_kinds),
                "metadata_keys": list(self.metadata_keys), "parameter_contract": plain(self.parameter_contract),
                "answer_format": self.answer_format, "answer_contract": plain(self.answer_contract)}

    @classmethod
    def load(cls, path: Path, bundle=None):
        data = yaml.safe_load(Path(path).read_text())
        return cls(**data, bundle=bundle)

    def with_bundle(self, bundle):
        from dataclasses import replace
        return replace(self, bundle=bundle)

    def validate_input(self, case):
        for block in case.corpus:
            if block.source.kind not in self.source_kinds or set(block.metadata) - set(self.metadata_keys):
                raise ValueError("Undeclared corpus layer or metadata")
        for q in case.questions:
            validate_value(q.parameters, self.parameter_contract, "question.parameters")
