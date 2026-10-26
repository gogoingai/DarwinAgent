"""Checked UTF-8 edits within one local task workspace."""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from darwinagent.agent import ToolRejectedBeforeEffect

MAX_FILE_BYTES = 2_000_000


class EditFailure(ToolRejectedBeforeEffect):
    public_codes = frozenset(
        {
            "invalid_edit_arguments",
            "invalid_read_arguments",
            "read_stale_file",
            "workspace_path_rejected",
            "edit_file_limit",
            "edit_stale_file",
            "edit_match_not_unique",
            "edit_invalid_utf8",
        }
    )


def scoped_file(workspace, name):
    root = Path(workspace)
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise EditFailure("workspace_path_rejected")
    if any(p.is_symlink() for p in root.rglob("*")):
        raise EditFailure("workspace_path_rejected")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise EditFailure("workspace_path_rejected")
    return path


def read_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise EditFailure("edit_file_limit")
    return data


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EditFailure("edit_invalid_utf8") from None


def read_text(workspace, params):
    if (
        set(params) - {"path", "offset", "limit", "expected_sha256"}
        or not isinstance(params.get("path"), str)
        or type(params.get("offset", 0)) is not int
        or params.get("offset", 0) < 0
        or type(params.get("limit", 128000)) is not int
        or not 1 <= params.get("limit", 128000) <= 128000
        or (
            "expected_sha256" in params
            and (
                not isinstance(params["expected_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", params["expected_sha256"]) is None
            )
        )
    ):
        raise EditFailure("invalid_read_arguments")
    data = read_bytes(scoped_file(workspace, params["path"]))
    checksum = hashlib.sha256(data).hexdigest()
    if params.get("expected_sha256", checksum) != checksum:
        raise EditFailure("read_stale_file")
    text = decode(data)
    legacy = {"path": params["path"], "content": text, "sha256": checksum}
    if (
        not (set(params) - {"path"})
        and len(json.dumps(legacy, ensure_ascii=False).encode()) < 160000
    ):
        return legacy
    offset = params.get("offset", 0)
    if offset > len(text):
        raise EditFailure("invalid_read_arguments")
    end = min(len(text), offset + params.get("limit", 128000))
    content = text[offset:end]
    # Account for JSON escaping as well as UTF-8; never split a Unicode character.
    while len(json.dumps(content, ensure_ascii=False).encode()) > 120000:
        end = offset + max(1, (end - offset) // 2)
        content = text[offset:end]
    return {
        "path": params["path"],
        "content": content,
        "sha256": checksum,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "offset": offset,
        "end_offset": end,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }


def replace_text(workspace, params):
    if (
        set(params) != {"path", "expected_sha256", "old", "new"}
        or any(not isinstance(value, str) for value in params.values())
        or not params["old"]
        or re.fullmatch(r"[0-9a-f]{64}", params["expected_sha256"]) is None
    ):
        raise EditFailure("invalid_edit_arguments")
    path = scoped_file(workspace, params["path"])
    data = read_bytes(path)
    if hashlib.sha256(data).hexdigest() != params["expected_sha256"]:
        raise EditFailure("edit_stale_file")
    content = decode(data)
    match = content.find(params["old"])
    if match < 0 or content.find(params["old"], match + 1) >= 0:
        raise EditFailure("edit_match_not_unique")
    output = content.replace(params["old"], params["new"], 1).encode("utf-8")
    if len(output) > MAX_FILE_BYTES:
        raise EditFailure("edit_file_limit")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        if hashlib.sha256(read_bytes(path)).hexdigest() != params["expected_sha256"]:
            raise EditFailure("edit_stale_file")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {
        "path": params["path"],
        "sha256": hashlib.sha256(output).hexdigest(),
        "bytes": len(output),
        "replacements": 1,
    }
