"""Versioned S/F/C/P/H assets; evaluation is never a kernel asset."""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from oak.runtime import atomic_json, digest
from .harness import Harness

KINDS = {"schema", "functions", "checks", "prompts", "harness"}


def fp(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class KernelAssets:
    """一次运行所用内核资产的登记与指纹。"""
    schema_path: Path | None = None
    prompt_paths: dict[str, Path] = field(default_factory=dict)   # role -> 文件
    function_paths: list[Path] = field(default_factory=list)
    check_ids: list[str] = field(default_factory=list)           # checks.py 注册表里的 id
    check_paths: list[Path] = field(default_factory=list)
    harness_path: Path | None = None
    dependency_paths: list[Path] = field(default_factory=list)
    version: str = "1"
    scope: str = "domain"

    def manifest(self) -> dict:
        if self.schema_path is None:
            raise ValueError("Schema asset is required")
        if self.scope not in {"universal", "domain", "dataset"}:
            raise ValueError("Unknown asset scope")
        for role in self.prompt_paths:
            if not role or role in {".", ".."} or "/" in role or "\\" in role:
                raise ValueError("Prompt IDs must be single path components")
        def asset(path):
            return {"name": path.name, "fp": fp(path)}
        value = {
            "format_version": 2, "version": self.version, "scope": self.scope,
            "schema": asset(self.schema_path),
            "prompts": {r: asset(p) for r, p in self.prompt_paths.items()},
            "functions": [asset(p) for p in self.function_paths],
            "checks": {"ids": sorted(self.check_ids), "implementations": [asset(p) for p in self.check_paths]},
            "harness": asset(self.harness_path) if self.harness_path else None,
            "dependencies": [asset(p) for p in self.dependency_paths],
        }
        return {**value, "digest": digest(value)}

    def write_manifest(self, dest: Path) -> None:
        atomic_json(dest, self.manifest())

    def export(self, dest: Path) -> "KernelBundle":
        if dest.exists() and any(dest.iterdir()):
            raise ValueError("Export target must be empty")
        self.manifest()
        dest.mkdir(parents=True, exist_ok=True)
        assets = [("schema", "schema", self.schema_path)]
        assets += [("prompts", role, path) for role, path in self.prompt_paths.items()]
        assets += [("functions", str(i), path) for i, path in enumerate(self.function_paths)]
        assets += [("checks", str(i), path) for i, path in enumerate(self.check_paths)]
        assets += [("dependency", str(i), path) for i, path in enumerate(self.dependency_paths)]
        if self.harness_path:
            assets.append(("harness", "harness", self.harness_path))
        records = []
        for kind, aid, path in assets:
            target = dest / "assets" / kind / f"{aid}{path.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            records.append({"kind": kind, "id": aid, "path": target.relative_to(dest).as_posix(), "fp": fp(target)})
        data = {"format_version": 2, "version": self.version, "scope": self.scope,
                "check_ids": sorted(self.check_ids), "assets": records}
        atomic_json(dest / "manifest.json", {**data, "digest": digest(data)})
        return KernelBundle.load(dest)


@dataclass(frozen=True)
class KernelBundle:
    root: Path
    manifest_text: str

    @classmethod
    def load(cls, root: Path):
        bundle = cls(Path(root).resolve(), (Path(root) / "manifest.json").read_text())
        bundle.verify()
        return bundle

    @property
    def manifest(self):
        return json.loads(self.manifest_text)

    def verify(self):
        value = self.manifest
        if value.get("format_version") != 2:
            raise ValueError("Unsupported bundle format")
        identity = value.pop("digest")
        if digest(value) != identity:
            raise ValueError("Manifest digest mismatch")
        seen = set()
        for asset in value["assets"]:
            key = (asset["kind"], asset["id"])
            if asset["kind"] not in KINDS | {"dependency"} or key in seen:
                raise ValueError("Unknown or duplicate bundle asset")
            seen.add(key)
            path = (self.root / asset["path"]).resolve()
            if not path.is_relative_to(self.root) or fp(path) != asset["fp"]:
                raise ValueError(f"Invalid asset: {asset['id']}")
        if ("schema", "schema") not in seen:
            raise ValueError("Bundle requires a schema")

    def path(self, kind: str, aid: str):
        self.verify()
        asset = next(a for a in self.manifest["assets"] if a["kind"] == kind and a["id"] == aid)
        return self.root / asset["path"]

    def schema(self):
        from oak.schema.model import Schema
        schema = Schema.from_yaml(self.path("schema", "schema").read_text())
        if schema.validate():
            raise ValueError(schema.validate())
        return schema

    def harness(self):
        return Harness.from_dict(json.loads(self.path("harness", "harness").read_text()))


__all__ = ["KernelAssets", "KernelBundle", "Harness", "fp", "KINDS"]
