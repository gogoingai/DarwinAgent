"""Relocatable, fingerprinted S/F/C/P bundles; permissions live in framework code."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Mapping

from oak.contracts import freeze, plain
from oak.runtime.artifacts import atomic_json, digest
from .spec import KINDS, PROMPT_SLOTS, RESERVED_PREFIX

FORMAT_VERSION = 3
CAPABILITY_VERSION = "restricted-python-1"


@dataclass(frozen=True)
class Asset:
    id: str
    kind: str
    content: str
    input_contract: Mapping = field(default_factory=lambda: {"type": "object", "properties": {}})
    output_contract: Mapping = field(default_factory=lambda: {"type": "any"})
    schema_dependencies: tuple[str, ...] = ()
    role: str = ""
    stage: str = ""
    description: str = ""
    trial_inputs: tuple[Mapping, ...] = ()

    def __post_init__(self):
        if self.kind not in KINDS or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", self.id):
            raise ValueError("Invalid asset type or id; only S/F/C/P are assets")
        if self.id.startswith(RESERVED_PREFIX) or not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Reserved check id or empty asset")
        object.__setattr__(self, "input_contract", freeze(self.input_contract))
        object.__setattr__(self, "output_contract", freeze(self.output_contract))
        object.__setattr__(self, "schema_dependencies", tuple(self.schema_dependencies))
        object.__setattr__(self, "trial_inputs", tuple(freeze(x) for x in self.trial_inputs))
        if self.kind == "F" and not self.trial_inputs:
            raise ValueError("Every F requires registered trial parameters")
        if self.kind == "P":
            if self.role not in PROMPT_SLOTS:
                raise ValueError("Unregistered prompt role")
            template = Template(self.content)
            if not template.is_valid() or set(template.get_identifiers()) - PROMPT_SLOTS[self.role]:
                raise ValueError("Prompt refers to an unregistered slot")
        elif self.role:
            raise ValueError("Only prompts have roles")
        if self.kind == "C" and self.stage not in {"graph", "answer"}:
            raise ValueError("Task checks require a fixed stage")
        if self.kind != "C" and self.stage:
            raise ValueError("Only checks have stages")

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "content": self.content,
                "input_contract": plain(self.input_contract), "output_contract": plain(self.output_contract),
                "schema_dependencies": list(self.schema_dependencies), "role": self.role,
                "stage": self.stage, "description": self.description, "trial_inputs": plain(self.trial_inputs)}

    @property
    def fingerprint(self):
        return digest(self.to_dict())


@dataclass(frozen=True)
class KernelAssets:
    assets: tuple[Asset, ...]
    origin: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "assets", tuple(self.assets))
        object.__setattr__(self, "origin", freeze(self.origin))
        ids = [a.id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate asset ids")
        schemas = {a.id for a in self.assets if a.kind == "S"}
        if len(schemas) != 1:
            raise ValueError("Exactly one schema asset required")
        roles = [a.role for a in self.assets if a.kind == "P"]
        if sorted(roles) != sorted(PROMPT_SLOTS):
            raise ValueError("Exactly one prompt per fixed role required")
        if not any(a.kind == "F" for a in self.assets) or not any(a.kind == "C" for a in self.assets):
            raise ValueError("Functions and task checks are required")
        for a in self.assets:
            if a.kind != "S" and set(a.schema_dependencies) != schemas:
                raise ValueError(f"{a.id}: schema dependency must reference the registered schema")

    def manifest(self):
        rows = []
        for a in sorted(self.assets, key=lambda x: x.id):
            ext = {"S": "yaml", "F": "py", "C": "py", "P": "txt"}[a.kind]
            rows.append({**a.to_dict(), "content": None, "path": f"assets/{a.kind}/{a.id}.{ext}",
                         "sha256": hashlib.sha256(a.content.encode()).hexdigest(), "fingerprint": a.fingerprint})
        data = {"format_version": FORMAT_VERSION, "capability_version": CAPABILITY_VERSION,
                "assets": rows, "origin": plain(self.origin)}
        return {**data, "version": digest(data)}

    def export(self, root: Path):
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise ValueError("Cannot overwrite an existing bundle")
        root.mkdir(parents=True, exist_ok=True)
        manifest = self.manifest()
        by_id = {a.id: a for a in self.assets}
        for row in manifest["assets"]:
            path = root / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(by_id[row["id"]].content)
        atomic_json(root / "manifest.json", manifest)
        return KernelBundle(root)

    @classmethod
    def from_payload(cls, payload, origin=None):
        if set(payload) != {"assets"} or not isinstance(payload["assets"], list):
            raise ValueError("Expected an asset package, with no paths or permissions")
        return cls(tuple(Asset(**item) for item in payload["assets"]), origin or {})


class KernelBundle:
    def __init__(self, root: Path):
        import json
        self.root = Path(root).resolve()
        self._manifest = json.loads((self.root / "manifest.json").read_text())
        self.version = self._manifest["version"]
        self.verify()

    def verify(self):
        import json
        manifest = json.loads((self.root / "manifest.json").read_text())
        if manifest != self._manifest or digest({k: v for k, v in manifest.items() if k != "version"}) != self.version:
            raise ValueError("Asset manifest changed")
        if manifest["format_version"] != FORMAT_VERSION or manifest["capability_version"] != CAPABILITY_VERSION:
            raise ValueError("Unsupported asset contract version")
        assets = []
        for row in manifest["assets"]:
            path = (self.root / row["path"]).resolve()
            ext = {"S": "yaml", "F": "py", "C": "py", "P": "txt"}.get(row["kind"])
            expected = f"assets/{row['kind']}/{row['id']}.{ext}"
            if row["path"] != expected or not path.is_relative_to(self.root) or path.is_symlink():
                raise ValueError("Asset path outside registered location")
            content = path.read_text()
            if hashlib.sha256(content.encode()).hexdigest() != row["sha256"]:
                raise ValueError(f"Asset tampered: {row['id']}")
            a = Asset(**{k: v for k, v in row.items() if k not in {"path", "sha256", "fingerprint", "content"}}, content=content)
            if a.fingerprint != row["fingerprint"]:
                raise ValueError("Asset contract tampered")
            assets.append(a)
        self.assets = KernelAssets(tuple(assets), manifest["origin"])

    def get(self, asset_id):
        self.verify()
        found = next((a for a in self.assets.assets if a.id == asset_id), None)
        if found is None:
            raise ValueError(f"Unregistered asset: {asset_id}")
        return found

    def path(self, asset_id):
        self.get(asset_id)
        return self.root / next(x["path"] for x in self._manifest["assets"] if x["id"] == asset_id)
