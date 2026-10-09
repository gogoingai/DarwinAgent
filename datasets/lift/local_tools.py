"""Context-owned local bindings of the unchanged credential-free fixed toolbox."""

import importlib.util
import json
import time
import uuid
from pathlib import Path

from darwinagent.contracts import plain
from darwinagent.runtime.artifacts import atomic_json

from .data import sha
from .tools import NAMES, DockerTools


class LocalTools(DockerTools):
    """Reuse declarations and pure primitives; each instance owns its toolbox root.

    No child process, arbitrary host code, inherited credentials or mutable module
    singleton is exposed. Judge bindings cannot dispatch write_text, even directly.
    """

    def __init__(self, workspace, readonly=False):
        super().__init__(None, workspace, readonly)
        self.source = Path(__file__).with_name("toolbox.py")
        self.source_hash = sha(self.source)
        spec = importlib.util.spec_from_file_location("lift_local_" + uuid.uuid4().hex, self.source)
        self.toolbox = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.toolbox)
        self.toolbox.ROOT = Path(self.workspace)

    async def invoke(self, name, params):
        if name not in NAMES:
            raise ValueError("Unknown primitive")
        if self.readonly and name == "write_text":
            raise PermissionError("Judge has read-only artifacts")
        if sha(self.source) != self.source_hash:
            raise ValueError("Frozen toolbox source changed")
        # Reject symlinks even when they point within the workspace. Paths and
        # listing are checked before opening any target, including dangling links.
        root = Path(self.workspace)
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise ValueError("Workspace symlink rejected")
        if any(p.is_symlink() for p in root.rglob("*")):
            raise ValueError("Workspace symlink rejected")
        started = time.time()
        try:
            result = self.toolbox.execute(name, plain(params))
            if len(json.dumps(result, ensure_ascii=False, default=str).encode()) > 180000:
                raise ValueError("Tool output exceeds 180000 bytes; narrow query")
            output = {"ok": True, "result": result}
        except Exception as exc:
            output = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            if self.audit_path is not None:
                receipt_id = uuid.uuid4().hex
                atomic_json(
                    self.audit_path / (receipt_id + ".json"),
                    {
                        "id": receipt_id,
                        "name": name,
                        "params": plain(params),
                        "output": output,
                        "started": started,
                        "finished": time.time(),
                    },
                )
        return result
