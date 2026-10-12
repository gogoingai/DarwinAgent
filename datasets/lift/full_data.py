"""Full pinned EALE dataset; public worker inputs and privileged judge inputs.

Construction is privileged: it opens all 84 task definitions. Only ``task`` and
``stage`` produce worker inputs. Native references are available through explicitly
split-scoped methods and must never be passed to the worker or proposal generator.
"""

import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath

from darwinagent.agent import AgentTask
from darwinagent.runtime.artifacts import atomic_json, digest

from .data import EALE_REVISION, LiftDataset, sha

SCENES = (
    "PPT制作攻略测评",
    "Sales_Ops_Weekly_Analytics_new",
    "Startup_BP_Investor_Roadshow_new",
    "Team_Building_Planning_new",
    "data_analysis",
    "information_search_gathering",
    "健康零食购物攻略测评",
    "周报自动生成",
    "商旅场景",
    "小红书产品运营场景",
    "旅游规划测试",
    "猫粮购买攻略测评",
    "股票投资决策",
    "英语语法学习攻略测评",
)


def _no_symlinks(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Dataset/workspace symlink rejected")


def _relative(value):
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Invalid manifest path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("Noncanonical manifest path")
    return path


class FullLiftDataset:
    """Separate full-data protocol; leaves historical LiftDataset bytes unchanged.

    ``train_ids`` includes q1..q4; callers reserve q4 for candidate validation.
    ``test_ids`` includes q5/q6 and cannot enter a train suite. Attachment staging
    retains upstream relative names inside a task-exclusive workspace. Optional
    pinned scene/skills are public upstream seeds for every arm and split; they
    are distinct from evolution-produced assets. The current full manifest has none.
    """

    manifest_name = "full-download-manifest.json"
    scenes = SCENES

    def __init__(self, data_root):
        from src.models import ExpectedResult, SuiteTask, TaskRequirements

        root = Path(data_root).absolute()
        _no_symlinks(root)
        self.root = root.resolve()
        manifest_path = self.root / self.manifest_name
        _no_symlinks(manifest_path)
        self._manifest_sha = sha(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("revision") != EALE_REVISION
            or manifest.get("scope") != "full_EALE"
            or manifest.get("test_visibility") != "privileged_dataset_only"
            or manifest.get("scene_counts") != {s: {"train": 4, "test": 2} for s in SCENES}
        ):
            raise ValueError("Unexpected full EALE protocol or scene roster")
        rows = manifest.get("files")
        if not isinstance(rows, list) or not rows:
            raise ValueError("Missing file manifest")
        self.files = tuple(dict(row) for row in rows)
        self._files_digest = digest(self.files)
        expected = {}
        definitions = {}
        directories = {}
        for row in self.files:
            relative = _relative(row.get("path"))
            checksum = row.get("sha256")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("Invalid pinned checksum")
            if str(relative) in expected:
                raise ValueError("Duplicate manifest path")
            parts = relative.parts
            if len(parts) < 4 or parts[0] != "benchmark_mds" or parts[1] not in SCENES:
                raise ValueError("File outside declared scene roster")
            scene = parts[1]
            if parts[2] == "skills":
                # Upstream skills live at scene/skills, separately from task gold.
                pass
            else:
                if parts[2] not in ("train", "test") or len(parts) < 5:
                    raise ValueError("File outside declared split")
                match = re.fullmatch(r"(q[1-6])_[^/]+", parts[3])
                if not match:
                    raise ValueError("Invalid task directory")
                question = match[1]
                split = "train" if question in ("q1", "q2", "q3", "q4") else "test"
                if parts[2] != split:
                    raise ValueError("Question in wrong split")
                key = (scene, question)
                task_dir = PurePosixPath(*parts[:4])
                if key in directories and directories[key] != task_dir:
                    raise ValueError("Duplicate scene question")
                directories[key] = task_dir
                if len(parts) == 5 and parts[4] == parts[3] + ".md":
                    if key in definitions:
                        raise ValueError("Duplicate task definition")
                    definitions[key] = str(relative)
                elif len(parts) < 6 or parts[4] not in (question + "_materials", "materials"):
                    raise ValueError("Only task definitions, materials and scene skills allowed")
            path = self.root / str(relative)
            _no_symlinks(path)
            if not path.is_file() or sha(path) != checksum:
                raise ValueError("Missing or changed pinned file")
            if "size" in row and (
                type(row["size"]) is not int or path.stat().st_size != row["size"]
            ):
                raise ValueError("Pinned size mismatch")
            expected[str(relative.relative_to("benchmark_mds"))] = checksum
        wanted = {(s, f"q{n}") for s in SCENES for n in range(1, 7)}
        if set(definitions) != wanted:
            raise ValueError("Full EALE requires exactly 14 x (4 train + 2 test) tasks")
        self._tree = expected
        self._native = {}
        self._metadata = {}
        self._attachments = {}
        for scene in SCENES:
            for number in range(1, 7):
                question = f"q{number}"
                key = (scene, question)
                task_id = (
                    "scene-" + hashlib.sha256(scene.encode()).hexdigest()[:16] + "-" + question
                )
                if task_id in self._native:
                    raise ValueError("Compound task ID collision")
                parts = re.split(
                    r"^### (query|要求|轨迹要求)\s*$",
                    (self.root / definitions[key]).read_text(encoding="utf-8"),
                    flags=re.M,
                )
                if parts[1::2] != ["query", "要求", "轨迹要求"] or parts[0].strip():
                    raise ValueError("Ambiguous native task sections")
                sections = dict(zip(parts[1::2], (p.strip() for p in parts[2::2])))
                if not all(sections.values()):
                    raise ValueError("Missing native task section")
                task_dir = self.root / str(directories[key])
                resources = []
                material_dirs = [
                    task_dir / name
                    for name in (question + "_materials", "materials")
                    if (task_dir / name).exists()
                ]
                if len(material_dirs) > 1:
                    raise ValueError("Ambiguous materials directory")
                materials = material_dirs[0] if material_dirs else None
                skills = self.root / "benchmark_mds" / scene / "skills"
                if materials:
                    resources.append(materials)
                if skills.exists():
                    resources.append(skills)
                attachments = {}
                for resource in resources:
                    for row in self.files:
                        source = self.root / row["path"]
                        if source.is_relative_to(resource):
                            public = resource.name + "/" + source.relative_to(resource).as_posix()
                            attachments[public] = (row["path"], row["sha256"])
                self._attachments[task_id] = attachments
                split = "train" if number <= 4 else "test"
                self._metadata[task_id] = (scene, question, split)
                self._native[task_id] = SuiteTask(
                    name=task_id,
                    query=sections["query"],
                    category_name=scene,
                    requirements=TaskRequirements(
                        material_dir=str(materials) if materials else None,
                        extra_skills_dir=str(skills) if skills.exists() else None,
                    ),
                    expected_result=ExpectedResult(
                        content_reqs=sections["要求"], trajectory_reqs=sections["轨迹要求"]
                    ),
                )
        self.ids = tuple(self._native)
        self.train_ids = tuple(t for t in self.ids if self._metadata[t][2] == "train")
        self.test_ids = tuple(t for t in self.ids if self._metadata[t][2] == "test")
        self.identity()

    def identity(self):
        """Verify immutable source manifest and complete source tree, without content output."""
        _no_symlinks(self.root / self.manifest_name)
        if (
            sha(self.root / self.manifest_name) != self._manifest_sha
            or digest(self.files) != self._files_digest
        ):
            raise ValueError("Pinned manifest changed")
        LiftDataset.verify_tree(self.root / "benchmark_mds", self._tree)
        return {
            "revision": EALE_REVISION,
            "manifest": self._manifest_sha,
            "protocol": "full_EALE",
            "scene_count": 14,
            "train_count": 56,
            "test_count": 28,
            "test_visibility": "privileged_dataset_only",
        }

    def task_ids(self, scene, split):
        if scene not in SCENES or split not in ("train", "test"):
            raise ValueError("Unknown scene or split")
        return tuple(t for t in self.ids if self._metadata[t][0::2] == (scene, split))

    def task_metadata(self, task_id):
        """Return only scene, upstream question number, and split."""
        scene, question, split = self._metadata[task_id]
        return {"scene": scene, "question": question, "split": split}

    def task(self, task_id):
        """Public query and hashed attachments; never includes expected_result."""
        self.identity()
        if task_id not in self._native:
            raise ValueError("Unknown compound task ID")
        attachments = tuple(
            {"path": path, "sha256": checksum}
            for path, (_, checksum) in sorted(self._attachments[task_id].items())
        )
        return AgentTask(task_id, self._native[task_id].query, attachments)

    def privileged_task(self, task_id, *, split):
        """Judge-only native task, with an explicit split check and defensive copy."""
        if split not in ("train", "test") or self._metadata.get(task_id, (None,) * 3)[2] != split:
            raise PermissionError("Native references require the matching explicit split")
        self.identity()
        return self._native[task_id].model_copy(deep=True)

    def stage(self, task, workspace):
        """Stage only this task's attachments into a dedicated, verified workspace."""
        if task.to_dict() != self.task(task.id).to_dict():
            raise ValueError("Public task or attachments changed")
        root = Path(workspace).absolute()
        _no_symlinks(root)
        root = root.resolve()
        expected = {p: checksum for p, (_, checksum) in self._attachments[task.id].items()}
        # Binding lives beside the public workspace, outside worker-readable inputs.
        # A resume may contain tool outputs, but pinned input directories stay exact.
        marker = root.parent / (".full-lift-stage-" + digest(str(root))[:24] + ".json")
        _no_symlinks(marker)
        binding = {
            "workspace": str(root),
            "task_id": task.id,
            "source_manifest": self._manifest_sha,
            "public_task": digest(task.to_dict()),
        }
        if marker.exists():
            if json.loads(marker.read_text(encoding="utf-8")) != binding:
                raise ValueError("Workspace bound to a different task or source")
            for path in root.rglob("*"):
                if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                    raise ValueError("Unsupported resumed workspace entry")
            for name in {PurePosixPath(p).parts[0] for p in expected}:
                LiftDataset.verify_tree(
                    root / name,
                    {
                        str(PurePosixPath(p).relative_to(name)): h
                        for p, h in expected.items()
                        if PurePosixPath(p).parts[0] == name
                    },
                    partial=True,
                )
        else:
            # First staging accepts only this task's intact/partially copied inputs.
            LiftDataset.verify_tree(root, expected, partial=True)
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(marker, binding)
        for relative, (source_relative, checksum) in self._attachments[task.id].items():
            source = self.root / source_relative
            destination = root / relative
            _no_symlinks(source)
            _no_symlinks(destination)
            if not destination.resolve().is_relative_to(root) or sha(source) != checksum:
                raise ValueError("Attachment boundary or hash mismatch")
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        for name in {PurePosixPath(p).parts[0] for p in expected}:
            LiftDataset.verify_tree(
                root / name,
                {
                    str(PurePosixPath(p).relative_to(name)): h
                    for p, h in expected.items()
                    if PurePosixPath(p).parts[0] == name
                },
            )

    def write_suite(self, path, scene, *, split):
        """Privileged native suite; train and test are never combined implicitly."""
        from src.models import Suite

        tasks = [self.privileged_task(t, split=split) for t in self.task_ids(scene, split)]
        suite = Suite(
            name=f"full_eale_{scene}_{split}",
            category=scene,
            warmup_tasks=tasks if split == "train" else [],
            holdout_tasks=tasks if split == "test" else [],
        )
        Path(path).write_text(suite.model_dump_json(indent=2), encoding="utf-8")
        return suite
