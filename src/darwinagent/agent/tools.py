"""Optional scoped file primitives. No host filesystem access is granted by assets."""

import hashlib
from pathlib import Path

from .contracts import ToolSpec


def file_tools(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def path_for(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("File path outside registered workspace")
        return path

    def read(params):
        path = path_for(params["path"])
        return {"path": str(path), "content": path.read_text()}

    def write(params):
        path = path_for(params["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params["content"])
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    path_contract = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    write_contract = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }
    return (
        ToolSpec(
            "read_file",
            read,
            path_contract,
            description="Read UTF-8 file in task workspace",
            identity={"root": str(root)},
        ),
        ToolSpec(
            "write_file",
            write,
            write_contract,
            effect="write",
            description="Write UTF-8 artifact in task workspace",
            identity={"root": str(root)},
        ),
    )
