"""Verified portable bundles and nondestructive import of legacy run evidence.

A bundle is a directory with relative file references, not a live database or
checkout. Legacy files stay byte-identical; absent dependencies remain explicit
rather than being invented. Import commits only records whose objects exist.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .artifacts import atomic_json, digest
from .workspace import Workspace, _safe

FORMAT = "darwinagent-workspace-1"
TABLES = (
    "provenance",
    "refs",
    "events",
    "branches",
    "requests",
    "request_replacements",
    "attempts",
)


def _secret_file(path, data):
    name = path.name.lower()
    if name in {"stop", "pause", ".env"} or name.endswith(
        (".lock", ".sqlite3-wal", ".sqlite3-shm")
    ):
        return True
    if name.startswith(".env.") or any(
        word in name for word in ("credential", "apikey", "api_key")
    ):
        return True
    text = data.decode("utf-8", errors="ignore")
    if re.search(
        r"(?im)^\s*(?:export\s+)?(?:[A-Z0-9_]*API_KEY|PASSWORD|ACCESS_TOKEN|AUTHORIZATION)\s*=\s*\S+",
        text,
    ):
        return True
    try:
        value = json.loads(data)
        _safe(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    except ValueError:
        return True
    return False


def _publish(destination, build):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Export destination already exists; original contents are preserved")
    temporary = Path(tempfile.mkdtemp(prefix=".export-", dir=destination.parent))
    try:
        report = build(temporary)
        os.rename(temporary, destination)
        Workspace._sync_directory(destination.parent)
        return report
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def export_workspace(workspace: Workspace, destination: Path):
    """Export a consistent DB snapshot and its complete object closure.

    All recorded objects are included, including drafts, rejected candidates,
    failures and prior branches. In-flight requests become unknown on import.
    """

    def build(temporary):
        with workspace._db() as db:
            db.execute("BEGIN")
            rows = [dict(row) for row in db.execute("SELECT * FROM objects ORDER BY id")]
            records = {
                table: [dict(row) for row in db.execute("SELECT * FROM " + table)]
                for table in TABLES
            }
        objects, missing, excluded = [], [], []
        (temporary / "objects").mkdir()
        for row in rows:
            try:
                data = workspace.read_bytes(row["id"])
                if _secret_file(Path(row["id"]), data):
                    excluded.append({"id": row["id"], "reason": "credential-bearing content"})
                    continue
                path = "objects/" + row["id"]
                (temporary / path).write_bytes(data)
                objects.append({**row, "path": path})
            except (OSError, ValueError, KeyError) as error:
                missing.append({"id": row["id"], "reason": str(error)})
        report = {
            "status": "partial" if missing or excluded else "complete",
            "exported": len(objects),
            "missing": missing,
            "excluded": excluded,
        }
        atomic_json(
            temporary / "manifest.json",
            {
                "format": FORMAT,
                "kind": "native",
                "objects": objects,
                "records": records,
                "report": report,
            },
        )
        return report

    return _publish(destination, build)


def _absolute_refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and Path(key).is_absolute():
                yield key
            yield from _absolute_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _absolute_refs(child)
    elif isinstance(value, str) and Path(value).is_absolute():
        yield value


def export_legacy_run(
    source: Path, destination: Path, *, path_map=None, exclude_roots=(), include_roots=()
):
    """Scan every legacy stage/round and recursively include referenced files.

    ``path_map`` maps old absolute file paths to accessible current locations.
    Evidence bytes are never rewritten to claim that missing provenance exists.
    """
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError("Legacy source directory does not exist")
    mapping = {str(key): Path(value).resolve() for key, value in (path_map or {}).items()}
    excluded_roots = tuple(Path(path).resolve() for path in exclude_roots)
    included_roots = tuple(Path(path).resolve() for path in include_roots)

    def build(temporary):
        (temporary / "objects").mkdir()
        pending = [
            (p, p.relative_to(source).as_posix())
            for p in sorted(source.rglob("*"))
            if p.is_file() and not p.is_symlink()
        ]
        visited, objects, missing, excluded = set(), [], [], []
        while pending:
            path, logical = pending.pop(0)
            if path in visited or (
                any(path.is_relative_to(root) for root in excluded_roots)
                and not any(path.is_relative_to(root) for root in included_roots)
            ):
                continue
            visited.add(path)
            try:
                data = path.read_bytes()
            except OSError as error:
                missing.append({"path": logical, "reason": str(error)})
                continue
            if _secret_file(path, data):
                excluded.append({"path": logical, "reason": "credential or active control file"})
                continue
            format = "legacy-bytes"
            object_id = hashlib.sha256(format.encode() + b"\0" + data).hexdigest()
            target = "objects/" + object_id
            (temporary / target).write_bytes(data)
            objects.append(
                {
                    "id": object_id,
                    "path": target,
                    "format": format,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "logical_path": logical,
                }
            )
            try:
                value = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            for reference in set(_absolute_refs(value)):
                dependency = mapping.get(reference, Path(reference))
                if dependency.is_file() and not dependency.is_symlink():
                    if dependency.is_relative_to(source):
                        label = dependency.relative_to(source).as_posix()
                    else:
                        # No host absolute paths in portable object references.
                        label = (
                            "external/"
                            + hashlib.sha256(reference.encode()).hexdigest()
                            + "/"
                            + dependency.name
                        )
                    pending.append((dependency, label))
                elif dependency.is_dir():
                    for child in sorted(dependency.rglob("*")):
                        if child.is_file() and not child.is_symlink():
                            label = (
                                "external/"
                                + hashlib.sha256(reference.encode()).hexdigest()
                                + "/"
                                + child.relative_to(dependency).as_posix()
                            )
                            pending.append((child, label))
                else:
                    missing.append(
                        {
                            "owner": logical,
                            "reference": reference,
                            "reason": "external dependency unavailable",
                        }
                    )
        report = {
            "status": "partial" if missing else "complete",
            "exported": len(objects),
            "missing": missing,
            "excluded": excluded,
        }
        atomic_json(
            temporary / "manifest.json",
            {
                "format": FORMAT,
                "kind": "legacy",
                "objects": objects,
                "records": {},
                "report": report,
            },
        )
        return report

    return _publish(destination, build)


def _validate_path(root, value):
    if not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("Bundle has an unsafe relative reference")
    path = root / value
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("Bundle reference escapes its directory")
    return path


def import_bundle(bundle: Path, workspace: Workspace):
    """Validate and stage objects before preserving compatible records.

    Corrupt/missing evidence and conflicting records are listed in the report;
    existing destination objects, branches and histories are never deleted.
    """
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise ValueError("Unsupported workspace bundle")
    rows = manifest.get("objects")
    if not isinstance(rows, list) or not isinstance(manifest.get("records"), dict):
        raise ValueError("Malformed workspace bundle")
    # Validate all references before writing any destination object.
    paths = [_validate_path(bundle, row["path"]) for row in rows]
    for row in rows:
        workspace.object_path(row["id"])
        if not isinstance(row.get("format"), str) or not row["format"]:
            raise ValueError("Malformed object format")
    imported, rejected, records_imported = [], [], []
    staged = Path(tempfile.mkdtemp(prefix=".import-", dir=workspace.root))
    try:
        valid = []
        for row, path in zip(rows, paths):
            try:
                data = path.read_bytes()
                if (
                    len(data) != row["size"]
                    or hashlib.sha256(data).hexdigest() != row["sha256"]
                    or hashlib.sha256(row["format"].encode() + b"\0" + data).hexdigest()
                    != row["id"]
                ):
                    raise ValueError("Object hash/format mismatch")
                if _secret_file(Path(row.get("logical_path", row["id"])), data):
                    raise ValueError("Credential or active control content")
                (staged / row["id"]).write_bytes(data)
                valid.append(row)
            except (OSError, ValueError, KeyError) as error:
                rejected.append({"id": row["id"], "reason": str(error)})
        for row in valid:
            try:
                workspace.put_bytes((staged / row["id"]).read_bytes(), format=row["format"])
                imported.append(row["id"])
                if manifest.get("kind") == "legacy":
                    workspace.add_provenance(
                        row["id"],
                        {
                            "kind": "legacy-import",
                            "logical_path": row["logical_path"],
                            "source": "unknown",
                        },
                    )
                    workspace.add_reference("legacy:" + row["logical_path"], "original", row["id"])
            except (OSError, ValueError) as error:
                rejected.append({"id": row["id"], "reason": str(error)})
        _import_records(workspace, manifest["records"], records_imported, rejected)
        for gap in manifest.get("report", {}).get("missing", []):
            rejected.append({"source_gap": gap})
        for gap in manifest.get("report", {}).get("excluded", []):
            if "id" in gap:  # credential exclusions from native closures are gaps
                rejected.append({"source_gap": gap})
        report = {
            "status": "partial" if rejected else "complete",
            "imported": imported,
            "records_imported": records_imported,
            "not_imported": rejected,
        }
        report_ref = workspace.put_json(report)
        workspace.append_event("import", {"report_ref": report_ref, "status": report["status"]})
        return report
    finally:
        shutil.rmtree(staged)


def _import_records(workspace, records, imported, rejected):
    """Foreign keys and exact row comparison prevent invented associations."""
    with workspace._db() as db:
        db.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            for original in records.get(table, []):
                row = dict(original)
                try:
                    for field in ("metadata", "payload", "receipt", "progress"):
                        if row.get(field):
                            _safe(json.loads(row[field]))
                    for reference_field in (
                        "object_id",
                        "payload_ref",
                        "response_ref",
                        "result_ref",
                    ):
                        if row.get(reference_field):
                            workspace.read_bytes(row[reference_field])
                    # Branch choices are version identifiers, not object foreign keys.
                    # Registered bundle dependency validation belongs to run export.
                    if table == "branches" and any(
                        row.get(field) is not None and not isinstance(row[field], str)
                        for field in ("adopted", "working")
                    ):
                        raise ValueError("Branch choices require independent string identifiers")
                    if table == "requests" and row["status"] == "submitted":
                        row["status"] = "unknown"
                    if table == "attempts" and row["status"] == "running":
                        row["status"] = "interrupted"
                    # Local event sequence is independent; preserve source sequence in payload.
                    if table == "events":
                        source_seq = row.pop("seq")
                        row["payload"] = json.dumps(
                            {
                                "imported_source_seq": source_seq,
                                "historical_payload": json.loads(row["payload"]),
                            }
                        )
                    keys = {
                        "refs": ("owner", "relation", "object_id"),
                        "branches": ("name",),
                        "request_replacements": ("original_id",),
                    }.get(table, ("id",))
                    if table != "events":
                        where = " AND ".join(key + "=?" for key in keys)
                        existing = db.execute(
                            "SELECT * FROM " + table + " WHERE " + where,
                            tuple(row[key] for key in keys),
                        ).fetchone()
                        if existing is not None:
                            if dict(existing) != row:
                                raise ValueError("Existing record has different content")
                            continue
                    # Restrict column names to schema, never interpolate untrusted identifiers.
                    allowed = {r["name"] for r in db.execute("PRAGMA table_info(" + table + ")")}
                    if set(row) - allowed:
                        raise ValueError("Unknown record fields")
                    columns = list(row)
                    db.execute(
                        "INSERT INTO "
                        + table
                        + "("
                        + ",".join(columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in columns)
                        + ")",
                        tuple(row[c] for c in columns),
                    )
                    imported.append({"table": table, "key": row.get("id", row.get("name"))})
                except (OSError, ValueError, KeyError, TypeError, sqlite3.IntegrityError) as error:
                    rejected.append(
                        {
                            "table": table,
                            "key": row.get("id", row.get("name")),
                            "reason": str(error),
                        }
                    )


def _registered_bundle_gaps(workspace, path_map):
    """Validate registered versions using their manifest namespace, never object IDs."""
    gaps = []
    for registration in sorted((workspace.root / "bundles").glob("*.json")):
        try:
            record = json.loads(registration.read_bytes())
            version = record["version"]
            if registration.stem != version:
                raise ValueError("Bundle registration version mismatch")
            root = Path(path_map.get(record["path"], record["path"])).resolve()
            manifest = json.loads((root / "manifest.json").read_bytes())
            if (
                manifest.get("version") != version
                or digest({key: value for key, value in manifest.items() if key != "version"})
                != version
            ):
                raise ValueError("Registered bundle manifest checksum mismatch")
            for asset in manifest["assets"]:
                path = _validate_path(root, asset["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                    raise ValueError("Registered bundle asset checksum mismatch")
        except (OSError, ValueError, KeyError, TypeError) as error:
            gaps.append(
                {
                    "registration": str(registration.relative_to(workspace.root.parent)),
                    "reason": str(error),
                }
            )
    return gaps


def export_run(root, destination, *, path_map=None):
    """Public portable export: native records plus all file-based run progress."""
    source_alias = str(Path(root).absolute())
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.is_relative_to(root):
        raise ValueError("Export destination must be outside the running experiment")
    workspace = Workspace(root / "workspace")

    def build(temporary):
        native = export_workspace(workspace, temporary / "native")
        progress = export_legacy_run(
            root,
            temporary / "progress",
            path_map=path_map,
            exclude_roots=(root / "workspace",),
            include_roots=(
                root / "workspace" / "bundles",
                root / "workspace" / "receipts",
                root / "workspace" / "candidate-drafts",
            ),
        )
        gaps = _registered_bundle_gaps(workspace, path_map or {})
        if gaps:
            progress["status"] = "partial"
            progress["missing"].extend(gaps)
            progress_manifest = json.loads((temporary / "progress" / "manifest.json").read_text())
            progress_manifest["report"] = progress
            atomic_json(temporary / "progress" / "manifest.json", progress_manifest)
        report = {
            "status": "partial"
            if "partial" in (native["status"], progress["status"])
            else "complete",
            "native": native,
            "progress": progress,
        }
        atomic_json(
            temporary / "run-manifest.json",
            {
                "format": "darwinagent-run-1",
                "source_root": str(root),
                "source_aliases": [source_alias, str(root)],
                "report": report,
            },
        )
        return report

    return _publish(destination, build)


def _relocate_graph_refs(value, source_root, target_root, external_paths, *, bundle_registry=False):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in {
                "graph_path",
                "case_path",
                "journal_path",
                "snapshot_path",
                "checkpoint_path",
                "workspace_root",
                "work_dir",
                "root",
            } or (bundle_registry and key == "path"):
                if not isinstance(child, str):
                    result[key] = child
                    continue
                original = Path(child)
                matching_root = next(
                    (
                        root
                        for root in source_root
                        if original.is_absolute() and original.is_relative_to(root)
                    ),
                    None,
                )
                if child in external_paths:
                    result[key] = str(external_paths[child])
                elif matching_root is not None:
                    result[key] = str(target_root / original.relative_to(matching_root))
                else:
                    result[key] = child
            else:
                result[key] = _relocate_graph_refs(
                    child, source_root, target_root, external_paths, bundle_registry=bundle_registry
                )
        return result
    if isinstance(value, list):
        return [
            _relocate_graph_refs(
                child, source_root, target_root, external_paths, bundle_registry=bundle_registry
            )
            for child in value
        ]
    return value


def _restore_registered_bundles(workspace, rows_by_label, source_roots, root, batch):
    """Materialize coherent version directories even when old target paths collide."""
    from .continuation import _immutable_copy

    paths, missing = {}, []
    for logical, registration_row in rows_by_label.items():
        if not logical.startswith("workspace/bundles/"):
            continue
        try:
            record = json.loads(workspace.read_bytes(registration_row["id"]))
            version, original_path = record["version"], record["path"]
            if Path(logical).stem != version or not re.fullmatch(r"[a-f0-9]{64}", version):
                raise ValueError("Invalid version registration")
            original = Path(original_path)
            matching = next(
                (source for source in source_roots if original.is_relative_to(source)), None
            )
            prefix = (
                original.relative_to(matching).as_posix() + "/"
                if matching
                else "external/" + hashlib.sha256(original_path.encode()).hexdigest() + "/"
            )
            manifest_row = rows_by_label[prefix + "manifest.json"]
            manifest_bytes = workspace.read_bytes(manifest_row["id"])
            manifest = json.loads(manifest_bytes)
            if (
                manifest.get("version") != version
                or digest({key: value for key, value in manifest.items() if key != "version"})
                != version
            ):
                raise ValueError("Imported version manifest checksum mismatch")
            destination = root / "imports" / batch / "bundles" / version
            staged = [("manifest.json", manifest_bytes)]
            for asset in manifest["assets"]:
                _validate_path(destination, asset["path"])
                data = workspace.read_bytes(rows_by_label[prefix + asset["path"]]["id"])
                if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                    raise ValueError("Imported version asset checksum mismatch")
                staged.append((asset["path"], data))
            for relative, data in staged:
                target = _validate_path(destination, relative)
                if target.exists() and target.read_bytes() != data:
                    raise ValueError("Existing registered version preserved; content conflicts")
            for relative, data in staged:
                _immutable_copy(destination / relative, data)
            paths[original_path] = destination
        except (OSError, KeyError, ValueError, TypeError) as error:
            missing.append({"registration": logical, "reason": str(error)})
    return paths, missing


def import_run(bundle, root):
    """Restore file progress with new location references and retain original bytes.

    Existing unequal target files are isolated in imports rather than replaced.
    Original identity/config/source metadata remain byte-identical archived
    objects; only operational graph_path references get a new current location.
    """
    bundle, root = Path(bundle).resolve(), Path(root).resolve()
    note = json.loads((bundle / "run-manifest.json").read_text())
    if note.get("format") != "darwinagent-run-1":
        raise ValueError("Unsupported run bundle")
    manifest = json.loads((bundle / "progress" / "manifest.json").read_text())
    for row in manifest["objects"]:
        _validate_path(root, row["logical_path"])
    workspace = Workspace(root / "workspace")
    native = import_bundle(bundle / "native", workspace)
    progress = import_bundle(bundle / "progress", workspace)
    manifest = json.loads((bundle / "progress" / "manifest.json").read_text())
    source_root = tuple(Path(value) for value in note.get("source_aliases", [note["source_root"]]))
    batch = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
    restored, isolated = [], []
    external_paths = {}
    # Resolve external graph references using their recorded portable labels.
    rows_by_label = {row["logical_path"]: row for row in manifest["objects"]}
    registered_paths, missing_bundles = _restore_registered_bundles(
        workspace, rows_by_label, source_root, root, batch
    )
    isolated.extend(missing_bundles)
    for row in manifest["objects"]:
        try:
            value = json.loads(workspace.read_bytes(row["id"]))
            for reference in _absolute_refs(value):
                prefix = "external/" + hashlib.sha256(reference.encode()).hexdigest() + "/"
                label = prefix + Path(reference).name
                if label in rows_by_label:
                    external_paths[reference] = root / "imports" / batch / label
                elif any(candidate.startswith(prefix) for candidate in rows_by_label):
                    external_paths[reference] = root / "imports" / batch / prefix
        except (OSError, KeyError, ValueError, UnicodeDecodeError):
            pass
    # Bind dependents to an isolated imported graph when the target graph conflicts.
    for row in manifest["objects"]:
        logical = row["logical_path"]
        if logical.startswith("external/"):
            continue
        target = _validate_path(root, logical)
        try:
            original = workspace.read_bytes(row["id"])
            if target.exists() and target.read_bytes() != original:
                selected = root / "imports" / batch / "conflicts" / logical
            else:
                selected = target
            for source in source_root:
                external_paths[str(source / logical)] = selected
        except (OSError, KeyError, ValueError):
            pass
    external_paths.update(registered_paths)
    for row in manifest["objects"]:
        logical = row["logical_path"]
        destination = _validate_path(root, logical)
        if logical.startswith("external/"):
            destination = root / "imports" / batch / logical
        try:
            original = workspace.read_bytes(row["id"])
            data = original
            try:
                value = json.loads(original)
                relocated = _relocate_graph_refs(
                    value,
                    source_root,
                    root,
                    external_paths,
                    bundle_registry=logical.startswith("workspace/bundles/"),
                )
                if relocated != value:
                    data = json.dumps(relocated, ensure_ascii=False, indent=2).encode() + b"\n"
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            if destination.exists() and destination.read_bytes() != data:
                isolated_path = root / "imports" / batch / "conflicts" / logical
                isolated_path.parent.mkdir(parents=True, exist_ok=True)
                if not isolated_path.exists():
                    isolated_path.write_bytes(data)
                isolated.append(
                    {
                        "path": logical,
                        "reason": "existing target preserved",
                        "isolated_path": str(isolated_path),
                        "original_ref": row["id"],
                    }
                )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".restore-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, destination)
                        Workspace._sync_directory(destination.parent)
                    except FileExistsError:
                        if destination.read_bytes() != data:
                            raise ValueError("Concurrent target update preserved") from None
                finally:
                    os.unlink(temporary)
            restored.append(
                {"path": logical, "original_ref": row["id"], "current_path": str(destination)}
            )
        except (OSError, ValueError, KeyError) as error:
            isolated.append({"path": logical, "reason": str(error)})
    report = {
        "status": "partial"
        if isolated or native["status"] == "partial" or progress["status"] == "partial"
        else "complete",
        "native": native,
        "progress": progress,
        "restored": restored,
        "isolated": isolated,
    }
    workspace.append_event(
        "run_import",
        {
            "report_ref": workspace.put_json(report),
            "source_batch": batch,
            "status": report["status"],
        },
    )
    return report
