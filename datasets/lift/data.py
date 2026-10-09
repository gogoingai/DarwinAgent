"""Privileged native task parsing and a minimal, hash-pinned public projection."""

import hashlib
import json
import re
import shutil
from pathlib import Path

from darwinagent.agent import AgentTask
from darwinagent.runtime.artifacts import digest

UPSTREAM_REVISION = "9d282e415c5e3b0f65b82c8758a013b269583d93"
EALE_REVISION = "419789ed1662e7b9ad16aa6620a4b31449612948"
IDS = ("q1", "q2", "q5")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LiftDataset:
    def __init__(self, data_root):
        from src.models import ExpectedResult, SuiteTask, TaskRequirements

        self.root = Path(data_root).resolve()
        manifest = json.loads((self.root / "download-manifest.json").read_text())
        if manifest["revision"] != EALE_REVISION or manifest.get("q6") != "not_downloaded":
            raise ValueError("Unexpected EALE revision or sealed task state")
        self.files = manifest["files"]
        self.native = {}
        for row in self.files:
            path = self.root / row["path"]
            if not path.resolve().is_relative_to(self.root) or path.is_symlink():
                raise ValueError("Dataset file escapes its root")
            if not re.search(r"/q[125]_[^/]+/", row["path"]):
                raise ValueError("Only q1/q2/q5 may be opened")
            if sha(path) != row["sha256"]:
                raise ValueError("Dataset material changed")
            if path.suffix == ".md":
                parts = re.split(r"^### (query|要求|轨迹要求)\s*$", path.read_text(), flags=re.M)
                sections = dict(zip(parts[1::2], (v.strip() for v in parts[2::2])))
                task_id = path.stem.split("_")[0]
                self.native[task_id] = SuiteTask(
                    name=task_id,
                    query=sections["query"],
                    category_name="sales_ops_development",
                    requirements=TaskRequirements(
                        material_dir=str(path.parent / (task_id + "_materials"))
                    ),
                    expected_result=ExpectedResult(
                        content_reqs=sections["要求"], trajectory_reqs=sections["轨迹要求"]
                    ),
                )
        if set(self.native) != set(IDS):
            raise ValueError("Development dataset must contain exactly q1/q2/q5")

    def identity(self):
        for row in self.files:
            if sha(self.root / row["path"]) != row["sha256"]:
                raise ValueError("Pinned dataset changed")
        return {
            "revision": EALE_REVISION,
            "manifest": digest(self.files),
            "q6": "sealed_not_opened",
        }

    def task(self, task_id):
        if task_id not in IDS:
            raise ValueError("Task outside authorized development split")
        native = self.native[task_id]
        material = Path(native.requirements.material_dir)
        attachments = tuple(
            {"path": material.name + "/" + str(p.relative_to(material)), "sha256": sha(p)}
            for p in sorted(material.rglob("*"))
            if p.is_file()
        )
        return AgentTask(task_id, native.query, attachments)

    def stage(self, task, workspace):
        if task.to_dict() != self.task(task.id).to_dict():
            raise ValueError("Public task or attachments changed")
        root = Path(workspace).resolve()
        root.mkdir(parents=True, exist_ok=True)
        material = Path(self.native[task.id].requirements.material_dir)
        for item in task.attachments:
            relative = Path(item["path"])
            destination = root / relative
            source = material / relative.relative_to(material.name)
            if not destination.resolve().is_relative_to(root) or sha(source) != item["sha256"]:
                raise ValueError("Attachment boundary or hash mismatch")
            if destination.exists():
                if sha(destination) != item["sha256"]:
                    raise ValueError("Existing staged attachment changed")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

    def write_suite(self, path):
        from src.models import Suite

        suite = Suite(
            name="darwin_development",
            category="sales_ops_development",
            warmup_tasks=[self.native["q1"]],
            holdout_tasks=[self.native["q5"]],
        )
        Path(path).write_text(suite.model_dump_json(indent=2))
        return suite
