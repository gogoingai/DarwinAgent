"""Version 4 assets use an immutable host execution profile, outside mutable S/F/C/P."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

import yaml

from darwinagent.contracts import freeze, plain
from darwinagent.kernel.spec import contract_errors, validate_value
from darwinagent.operators.sandbox import admit
from darwinagent.runtime.artifacts import atomic_json, digest

from .contracts import ExecutionProfile

CAPABILITY_VERSION = "agent-async-1"


@dataclass(frozen=True)
class AgentAsset:
    id: str
    kind: str
    content: str
    input_contract: Mapping = field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": True}
    )
    output_contract: Mapping = field(default_factory=lambda: {"type": "any"})
    schema_dependencies: tuple[str, ...] = ()
    role: str = ""
    stage: str = ""
    description: str = ""
    trial_inputs: tuple[Mapping, ...] = ()

    def __post_init__(self):
        if self.kind not in {"S", "F", "C", "P"} or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.-]{0,79}", self.id
        ):
            raise ValueError("Invalid v4 asset")
        if self.id.startswith("fixed.") or not self.content.strip():
            raise ValueError("Reserved id or empty content")
        if (self.kind == "P") != bool(self.role) or (self.kind == "C") != bool(self.stage):
            raise ValueError("Roles belong to P and stages belong to C")
        for name in ("input_contract", "output_contract"):
            if contract_errors(getattr(self, name)):
                raise ValueError("Invalid asset contract")
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "schema_dependencies", tuple(self.schema_dependencies))
        object.__setattr__(self, "trial_inputs", tuple(freeze(x) for x in self.trial_inputs))

    def to_dict(self):
        return {k: plain(v) for k, v in self.__dict__.items()}

    @property
    def fingerprint(self):
        return digest(self.to_dict())


@dataclass(frozen=True)
class AgentKernelAssets:
    assets: tuple[AgentAsset, ...]
    profile: ExecutionProfile
    origin: Mapping = field(default_factory=dict)
    profile_declaration: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "assets", tuple(self.assets))
        object.__setattr__(self, "origin", freeze(self.origin))
        declaration = self.profile_declaration or self.profile.declaration()
        if {k: v for k, v in plain(declaration).items() if k != "host_checks"} != {
            k: v for k, v in self.profile.declaration().items() if k != "host_checks"
        }:
            raise ValueError("Pinned profile cannot change host capabilities")
        object.__setattr__(self, "profile_declaration", freeze(declaration))
        ids = [a.id for a in self.assets]
        schemas = {a.id for a in self.assets if a.kind == "S"}
        if len(ids) != len(set(ids)) or len(schemas) != 1:
            raise ValueError("Unique ids and exactly one S required")
        roles = [a.role for a in self.assets if a.kind == "P"]
        if sorted(roles) != sorted(self.profile.prompt_roles):
            raise ValueError("Exactly one P per host role required")
        if not any(a.kind == "F" for a in self.assets):
            raise ValueError("At least one F required")
        for a in self.assets:
            if a.kind != "S" and set(a.schema_dependencies) != schemas:
                raise ValueError("Every asset depends on S")
            if a.kind == "F":
                admit(a.content, capability_names={"call", "view"})
                if not a.trial_inputs:
                    raise ValueError("F requires trial parameters")
                for trial in a.trial_inputs:
                    validate_value(trial, a.input_contract, "trial_input")
            if a.kind == "C":
                if a.stage not in self.profile.check_stages:
                    raise ValueError("Unknown lifecycle stage")
                admit(a.content, "C")
            if a.kind == "P":
                template = Template(a.content)
                if not template.is_valid() or set(template.get_identifiers()) - set(
                    self.profile.prompt_roles[a.role]
                ):
                    raise ValueError("Unregistered prompt slot")
        from .graph import validate_schema

        validate_schema(yaml.safe_load(next(a.content for a in self.assets if a.kind == "S")))

    @property
    def content_id(self):
        return digest(
            {
                "format_version": 4,
                "capability_version": CAPABILITY_VERSION,
                "profile": plain(self.profile_declaration),
                "assets": [a.to_dict() for a in sorted(self.assets, key=lambda a: a.id)],
            }
        )

    def manifest(self):
        rows = []
        for a in sorted(self.assets, key=lambda a: a.id):
            ext = {"S": "yaml", "F": "py", "C": "py", "P": "txt"}[a.kind]
            rows.append(
                {
                    **a.to_dict(),
                    "content": None,
                    "path": f"assets/{a.kind}/{a.id}.{ext}",
                    "sha256": hashlib.sha256(a.content.encode()).hexdigest(),
                    "fingerprint": a.fingerprint,
                }
            )
        data = {
            "format_version": 4,
            "capability_version": CAPABILITY_VERSION,
            "profile": plain(self.profile_declaration),
            "assets": rows,
            "origin": plain(self.origin),
        }
        return {**data, "version": digest(data)}

    def export(self, root):
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise ValueError("Cannot overwrite a bundle")
        root.mkdir(parents=True, exist_ok=True)
        manifest = self.manifest()
        for row in manifest["assets"]:
            path = root / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(next(a.content for a in self.assets if a.id == row["id"]))
        atomic_json(root / "manifest.json", manifest)
        return AgentKernelBundle(root)

    @classmethod
    def from_payload(cls, payload, profile, origin=None):
        if set(payload) != {"assets"}:
            raise ValueError("Expected assets only; capabilities belong to the host")
        return cls(tuple(AgentAsset(**row) for row in payload["assets"]), profile, origin or {})


class AgentKernelBundle:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._manifest = json.loads((self.root / "manifest.json").read_text())
        self.version = self._manifest["version"]
        self.verify()

    def verify(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        if (
            manifest != self._manifest
            or digest({k: v for k, v in manifest.items() if k != "version"}) != self.version
        ):
            raise ValueError("Asset manifest changed")
        if manifest["format_version"] != 4 or manifest["capability_version"] != CAPABILITY_VERSION:
            raise ValueError("Unsupported v4 contract")
        profile = dict(manifest["profile"])
        # Host callbacks are never loaded from an asset package; only their pinned identities are persisted.
        profile.pop("host_checks")
        assets = []
        for row in manifest["assets"]:
            ext = {"S": "yaml", "F": "py", "C": "py", "P": "txt"}[row["kind"]]
            if row["path"] != f"assets/{row['kind']}/{row['id']}.{ext}":
                raise ValueError("Unregistered asset path")
            raw = self.root / row["path"]
            if raw.is_symlink() or not raw.resolve().is_relative_to(self.root):
                raise ValueError("Asset escapes bundle")
            content = raw.read_text()
            asset = AgentAsset(
                **{
                    k: v
                    for k, v in row.items()
                    if k not in {"content", "path", "sha256", "fingerprint"}
                },
                content=content,
            )
            if (
                hashlib.sha256(content.encode()).hexdigest() != row["sha256"]
                or asset.fingerprint != row["fingerprint"]
            ):
                raise ValueError("Asset changed")
            assets.append(asset)
        self.assets = AgentKernelAssets(
            tuple(assets), ExecutionProfile(**profile), manifest["origin"], manifest["profile"]
        )
        self.profile_declaration = plain(self.assets.profile_declaration)

    @property
    def content_id(self):
        self.verify()
        return digest(
            {
                "format_version": 4,
                "capability_version": CAPABILITY_VERSION,
                "profile": self.profile_declaration,
                "assets": [a.to_dict() for a in sorted(self.assets.assets, key=lambda a: a.id)],
            }
        )

    def get(self, asset_id):
        self.verify()
        return next((a for a in self.assets.assets if a.id == asset_id), None) or self._missing(
            asset_id
        )

    @staticmethod
    def _missing(asset_id):
        raise ValueError(f"Unregistered asset: {asset_id}")

    def path(self, asset_id):
        self.get(asset_id)
        return self.root / next(
            row["path"] for row in self._manifest["assets"] if row["id"] == asset_id
        )
