"""Declarative task and asset contracts. No execution callbacks or capability grants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from darwinagent.contracts import freeze, plain

KINDS = frozenset({"S", "F", "C", "P"})
PROMPT_SLOTS = {
    "extract": frozenset({"schema"}),
    "tools": frozenset({"schema", "tools"}),
    "answer": frozenset({"schema"}),
    "review": frozenset({"schema"}),
}
RESERVED_PREFIX = "fixed."


def validate_value(value, contract, path="value"):
    """Small, closed JSON contract language; unsupported constraints are rejected."""
    if not isinstance(contract, Mapping):
        raise ValueError("Contract must be an object")
    if set(contract) - {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "additionalProperties",
        "description",
    }:
        raise ValueError("Unsupported contract keyword")
    types = {
        "object": lambda x: isinstance(x, Mapping),
        "array": lambda x: isinstance(x, (tuple, list)),
        "string": lambda x: type(x) is str,
        "integer": lambda x: type(x) is int,
        "number": lambda x: type(x) in (int, float),
        "boolean": lambda x: type(x) is bool,
        "null": lambda x: x is None,
        "any": lambda x: True,
    }
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


def contract_errors(contract, path="contract", limit=24):
    """Report every invalid declaration before executing data trials (closed language)."""
    errors = []
    allowed = {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "additionalProperties",
        "description",
    }
    kinds = {"object", "array", "string", "integer", "number", "boolean", "null", "any"}

    def walk(spec, where):
        if len(errors) >= limit:
            return
        if not isinstance(spec, Mapping):
            errors.append(f"{where}: Contract must be an object")
            return
        extra = set(spec) - allowed
        if extra:
            errors.append(f"{where}: Unsupported contract keyword {sorted(extra)}")
        kind = spec.get("type")
        if not isinstance(kind, str) or kind not in kinds:
            errors.append(
                f"{where}: unsupported type {plain(kind)!r}; type must be one string, not a union/list; use a supported type and normalize values"
            )
        props = spec.get("properties", {})
        if not isinstance(props, Mapping):
            errors.append(f"{where}.properties: must be an object")
        else:
            for key, value in props.items():
                walk(value, f"{where}.{key}")
        if "items" in spec:
            walk(spec["items"], f"{where}[]")

    walk(contract, path)
    return errors


@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    source_kinds: tuple[str, ...]
    metadata_keys: tuple[str, ...] = ()
    parameter_contract: Mapping = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    answer_format: str = "text"
    answer_contract: Mapping = field(default_factory=lambda: {"type": "string"})
    # 任务适配层声明的答案反例（结构电池 must-reject 行）：候选片段由任务给出，
    # 核心 zero 任务硬编码——语义上必须被拒的形态（如重复天序/空城市）由任务层
    # 声明，防止「结构合法」与「语义正确」混淆后模型删检查换过门
    # （2026-10-05 审查 P1：契约最小占位实例不得当作语义必过正例）。
    answer_counterexamples: tuple = ()
    # 任务适配层声明的正常 answered 正例（must_pass 行）：与问题/参数/证据匹配、
    # 语义上必须被接受的候选片段（二次复查 P1：占位实例结构档放行后，「只接受
    # 弃答、拒绝所有 answered」的 C 漏网——真实正例来自任务层，不用金标答案）。
    answer_examples: tuple = ()
    bundle: object | None = None
    seed_s: str = ""
    # 任务级硬约束（如 atomic_memory）：S 必须声明至少一个原子记忆节点类型，准入冻结。
    requirements: tuple = ()
    # 任务声明的检索工具底线（如 semantic_search/traversal）：F 集必须始终覆盖，冷启动准入
    # 与后续修订冻结都查（意图键在任务层，能力名映射见 kernel.validation）。
    retrieval_floor: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if not self.name or not self.description or not self.source_kinds:
            raise ValueError("Task requires a name, description and declared source kinds")
        if self.answer_format not in {"text", "json"}:
            raise ValueError("Unknown answer format")
        object.__setattr__(self, "parameter_contract", freeze(self.parameter_contract))
        object.__setattr__(self, "answer_contract", freeze(self.answer_contract))
        object.__setattr__(
            self,
            "answer_counterexamples",
            tuple(c for c in (self.answer_counterexamples or ()) if isinstance(c, Mapping)),
        )
        object.__setattr__(
            self,
            "answer_examples",
            tuple(c for c in (self.answer_examples or ()) if isinstance(c, Mapping)),
        )
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(
            self, "retrieval_floor", plain(self.retrieval_floor) if self.retrieval_floor else {}
        )
        if not isinstance(self.seed_s, str):
            raise ValueError("Seed schema must be text")
        if any(callable(x) for x in self.__dict__.values()):
            raise ValueError("Task cannot contain execution callbacks")

    def declaration(self):
        from darwinagent.runtime.artifacts import digest

        return {
            "name": self.name,
            "description": self.description,
            "source_kinds": list(self.source_kinds),
            "metadata_keys": list(self.metadata_keys),
            "parameter_contract": plain(self.parameter_contract),
            "answer_format": self.answer_format,
            "answer_contract": plain(self.answer_contract),
            "answer_counterexamples": [
                {"name": c.get("name", ""), "candidate_digest": digest(c.get("candidate", {}))}
                for c in plain(self.answer_counterexamples)
                if isinstance(c, Mapping)
            ],
            "answer_examples": [
                {"name": c.get("name", ""), "candidate_digest": digest(c.get("candidate", {}))}
                for c in plain(self.answer_examples)
                if isinstance(c, Mapping)
            ],
            "seed_s_fingerprint": digest(self.seed_s) if self.seed_s else "",
            "requirements": list(self.requirements),
            "retrieval_floor": plain(self.retrieval_floor),
        }

    @classmethod
    def load(cls, path: Path, bundle=None):
        path = Path(path)
        data = yaml.safe_load(path.read_text())
        seed = data.pop("seed", None) or {}
        seed_s = ""
        if isinstance(seed, Mapping) and seed.get("S"):
            seed_s = (path.parent / str(seed["S"])).read_text()
        return cls(**data, bundle=bundle, seed_s=seed_s)

    def with_bundle(self, bundle):
        from dataclasses import replace

        return replace(self, bundle=bundle)

    def validate_input(self, case):
        for block in case.corpus:
            if block.source.kind not in self.source_kinds or set(block.metadata) - set(
                self.metadata_keys
            ):
                raise ValueError("Undeclared corpus layer or metadata")
        for q in case.questions:
            validate_value(q.parameters, self.parameter_contract, "question.parameters")
