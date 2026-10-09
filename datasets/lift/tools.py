"""Fixed tool declarations bound to one native Docker environment."""

import json
import time
import uuid
from pathlib import Path

from darwinagent.agent import ToolSpec
from darwinagent.contracts import plain

NAMES = ("list_files", "read_text", "read_table", "transform_tables", "write_text")


class DockerTools:
    def __init__(self, container, workspace, readonly=False):
        self.audit_path = None
        self.container, self.workspace, self.readonly = (
            container,
            str(Path(workspace).resolve()),
            readonly,
        )

    def identity(self):
        # Container process names are transient. The persistent workspace is the resume identity.
        return {
            "workspace": self.workspace,
            "readonly": self.readonly,
            "toolbox_sha256": __import__("hashlib")
            .sha256(Path(__file__).with_name("toolbox.py").read_bytes())
            .hexdigest(),
        }

    async def invoke(self, name, params):
        from src.lift.adapters.container.exec import docker_exec_async

        if self.readonly and name == "write_text":
            raise PermissionError("Judge has read-only artifacts")
        started = time.time()
        value = await docker_exec_async(
            self.container,
            [
                "python",
                "/opt/darwin-tools/toolbox.py",
                json.dumps({"name": name, "params": plain(params)}, ensure_ascii=False),
            ],
            timeout_seconds=30,
        )
        result = json.loads(value)
        if self.audit_path is not None:
            from darwinagent.runtime.artifacts import atomic_json

            receipt_id = uuid.uuid4().hex
            atomic_json(
                self.audit_path / (receipt_id + ".json"),
                {
                    "id": receipt_id,
                    "name": name,
                    "params": plain(params),
                    "output": result,
                    "started": started,
                    "finished": time.time(),
                },
            )
        if not result["ok"]:
            raise ValueError(result["error_type"] + ": " + result["message"])
        return result["result"]

    async def list_files(self, params):
        return await self.invoke("list_files", params)

    async def read_text(self, params):
        return await self.invoke("read_text", params)

    async def read_table(self, params):
        return await self.invoke("read_table", params)

    async def transform_tables(self, params):
        return await self.invoke("transform_tables", params)

    async def write_text(self, params):
        return await self.invoke("write_text", params)

    def specs(self):
        descriptions = {
            "list_files": ("List current task workspace files", {}, []),
            "read_text": ("Read a UTF-8 file", {"path": {"type": "string"}}, ["path"]),
            "read_table": (
                "Read first XLSX sheet or CSV; date serials remain numeric",
                {"path": {"type": "string"}},
                ["path"],
            ),
            "transform_tables": (
                "Run read-only SQLite SELECT over named XLSX/CSV tables; tables maps aliases to paths; numeric text can be CAST, date serials use date(value + 2415018.5)",
                {
                    "tables": {"type": "object", "additionalProperties": True},
                    "sql": {"type": "string"},
                },
                ["tables", "sql"],
            ),
            "write_text": (
                "Write a UTF-8 result file (Markdown/CSV)",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
        }
        return tuple(
            ToolSpec(
                name,
                getattr(self, name),
                {"type": "object", "properties": props, "required": required},
                description=desc,
                effect="write" if name == "write_text" else "read",
                identity=self.identity(),
            )
            for name, (desc, props, required) in descriptions.items()
            if not (self.readonly and name == "write_text")
        )
