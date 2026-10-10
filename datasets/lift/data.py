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
    ids = IDS
    manifest_name = "download-manifest.json"
    manifest_q6 = "not_downloaded"
    path_pattern = r"/q[125]_[^/]+/"
    q6_status = "sealed_not_opened"

    def __init__(self, data_root):
        from src.models import ExpectedResult, SuiteTask, TaskRequirements

        self.root = Path(data_root).resolve()
        manifest = json.loads((self.root / self.manifest_name).read_text())
        if manifest["revision"] != EALE_REVISION or manifest.get("q6") != self.manifest_q6:
            raise ValueError("Unexpected EALE revision or sealed task state")
        self.files = manifest["files"]
        if len({r["path"] for r in self.files}) != len(self.files):
            raise ValueError("Duplicate manifest path")
        self.native = {}
        for row in self.files:
            path = self.root / row["path"]
            if (
                Path(row["path"]).is_absolute()
                or ".." in Path(row["path"]).parts
                or not path.resolve().is_relative_to(self.root)
                or any(p.is_symlink() for p in (path, *path.parents) if p != self.root)
            ):
                raise ValueError("Dataset file escapes its root")
            if not re.search(self.path_pattern, row["path"]):
                raise ValueError("File outside the declared authorized task split")
            if sha(path) != row["sha256"]:
                raise ValueError("Dataset material changed")
            if path.suffix == ".md" and not any(p.endswith("_materials") for p in path.parts):
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
        if set(self.native) != set(self.ids):
            raise ValueError("Dataset must contain exactly its declared task split")

        self.materials = {}
        for task_id, native in self.native.items():
            material = Path(native.requirements.material_dir)
            self.materials[task_id] = {
                str((self.root / r["path"]).relative_to(material)): r["sha256"]
                for r in self.files
                if (self.root / r["path"]).is_relative_to(material)
            }
        self.identity()

    @staticmethod
    def verify_tree(material, expected, *, partial=False):
        material = Path(material)
        if any(p.is_symlink() for p in (material, *material.parents)):
            raise ValueError("Material symlink rejected")
        actual = {}
        for path in material.rglob("*"):
            if path.is_symlink():
                raise ValueError("Material symlink rejected")
            if path.is_file():
                relative = str(path.relative_to(material))
                if relative not in expected:
                    raise ValueError("Unlisted material file")
                actual[relative] = sha(path)
            elif not path.is_dir():
                raise ValueError("Unsupported material entry")
        if (not partial and set(actual) != set(expected)) or any(
            value != expected[key] for key, value in actual.items()
        ):
            raise ValueError("Pinned material set or bytes changed")

    def identity(self):
        for row in self.files:
            path = self.root / row["path"]
            if any(p.is_symlink() for p in (path, *path.parents)) or sha(path) != row["sha256"]:
                raise ValueError("Pinned dataset changed")
        for task_id, native in self.native.items():
            self.verify_tree(native.requirements.material_dir, self.materials[task_id])
        return {
            "revision": EALE_REVISION,
            "manifest": digest(self.files),
            "q6": self.q6_status,
        }

    def task(self, task_id):
        if task_id not in self.ids:
            raise ValueError("Task outside authorized development split")
        native = self.native[task_id]
        material = Path(native.requirements.material_dir)
        self.verify_tree(material, self.materials[task_id])
        attachments = tuple(
            {"path": material.name + "/" + relative, "sha256": checksum}
            for relative, checksum in sorted(self.materials[task_id].items())
        )
        return AgentTask(task_id, native.query, attachments)

    def stage(self, task, workspace):
        self.identity()
        if task.to_dict() != self.task(task.id).to_dict():
            raise ValueError("Public task or attachments changed")
        root = Path(workspace).resolve()
        material = Path(self.native[task.id].requirements.material_dir)
        target = root / material.name
        self.verify_tree(target, self.materials[task.id], partial=True)
        root.mkdir(parents=True, exist_ok=True)
        for item in task.attachments:
            relative = Path(item["path"])
            destination = root / relative
            source = material / relative.relative_to(material.name)
            if not destination.resolve().is_relative_to(root) or sha(source) != item["sha256"]:
                raise ValueError("Attachment boundary or hash mismatch")
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        self.verify_tree(target, self.materials[task.id])

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


class LiftConfirmationDataset(LiftDataset):
    """Separately authorized q6-only manifest; never admitted to development training.

    Construction opens q6 files, so only use after the frozen development run and
    explicit confirmation-data authorization. Importing this class opens nothing.
    """

    ids = ("q6",)
    manifest_name = "confirmation-manifest.json"
    manifest_q6 = "authorized_confirmation"
    path_pattern = r"/q6_[^/]+/"
    q6_status = "independent_confirmation"

    def write_suite(self, path):
        raise PermissionError("Confirmation tasks cannot enter a development training suite")
