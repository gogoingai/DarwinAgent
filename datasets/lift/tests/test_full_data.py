"""Full-data privilege, native split, pinning and staging boundary regressions."""

import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.agent import AgentTask
from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.data import sha
from datasets.lift.full_data import SCENES, FullLiftDataset
from tests.support.full_lift_data import full_lift_fixture

try:
    import src.models  # noqa: F401
except ModuleNotFoundError:
    NATIVE_AVAILABLE = False
else:
    NATIVE_AVAILABLE = True


@unittest.skipUnless(NATIVE_AVAILABLE, "Pinned native LIFT environment required")
class FullDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "dataset"
        self.manifest = full_lift_fixture(self.root)

    def rewrite(self):
        atomic_json(self.root / FullLiftDataset.manifest_name, self.manifest)

    def test_exact_native_84_ids_and_split_projections(self):
        from src.models import Suite

        data = FullLiftDataset(self.root)
        self.assertEqual((len(data.ids), len(data.train_ids), len(data.test_ids)), (84, 56, 28))
        self.assertEqual(len(set(data.ids)), 84)
        self.assertEqual(data.ids, FullLiftDataset(self.root).ids)
        self.assertTrue(
            all(__import__("re").fullmatch(r"scene-[a-f0-9]{16}-q[1-6]", t) for t in data.ids)
        )
        for scene in SCENES:
            for split, numbers in (("train", (1, 2, 3, 4)), ("test", (5, 6))):
                ids = data.task_ids(scene, split)
                self.assertEqual(
                    [data.task_metadata(t)["question"] for t in ids], [f"q{n}" for n in numbers]
                )
                for task_id in ids:
                    native = data.privileged_task(task_id, split=split)
                    self.assertEqual(native.name, task_id)
                    self.assertEqual(native.category_name, scene)
                    with self.assertRaises(PermissionError):
                        data.privileged_task(task_id, split="test" if split == "train" else "train")
                path = Path(self.temporary.name).resolve() / f"{split}.json"
                suite = data.write_suite(path, scene, split=split)
                self.assertEqual(Suite.from_json_file(path), suite)
                self.assertEqual(len(suite.warmup_tasks), 4 if split == "train" else 0)
                self.assertEqual(len(suite.holdout_tasks), 2 if split == "test" else 0)

    def test_public_projection_and_stage_are_task_exclusive(self):
        data = FullLiftDataset(self.root)
        for task_id in data.ids:
            task = data.task(task_id)
            self.assertNotIn("GOLD_", json.dumps(task.to_dict()))
            workspace = Path(self.temporary.name).resolve() / "staged" / task.id
            data.stage(task, workspace)
            data.stage(task, workspace)  # identical resume is safe
            staged = [p for p in workspace.rglob("*") if p.is_file()]
            self.assertEqual(len(staged), len(task.attachments))
            self.assertFalse(any(p.suffix == ".md" for p in staged))
            for p in staged:
                self.assertNotIn("GOLD_", p.read_text())
                self.assertIn(data.task_metadata(task_id)["question"], p.read_text())
        first, second = data.task(data.train_ids[0]), data.task(data.train_ids[1])
        with self.assertRaises(ValueError):
            data.stage(second, Path(self.temporary.name).resolve() / "staged" / first.id)

    def test_bound_workspace_resume_preserves_outputs_and_rejects_other_task(self):
        data = FullLiftDataset(self.root)
        task = data.task(data.train_ids[0])
        workspace = Path(self.temporary.name).resolve() / "resume-workspace"
        data.stage(task, workspace)
        output = workspace / "result/result_q1/report.md"
        output.parent.mkdir(parents=True)
        output.write_text("legitimate tool output")
        before = {
            p.relative_to(workspace).as_posix(): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        data.stage(task, workspace)
        self.assertEqual(
            before,
            {
                p.relative_to(workspace).as_posix(): p.read_bytes()
                for p in workspace.rglob("*")
                if p.is_file()
            },
        )
        with self.assertRaises(ValueError):
            data.stage(data.task(data.train_ids[1]), workspace)
        input_path = workspace / task.attachments[0]["path"]
        input_path.write_text("changed input")
        with self.assertRaises(ValueError):
            data.stage(task, workspace)
        self.assertEqual(output.read_text(), "legitimate tool output")

    def test_native_copy_cannot_mutate_public_query(self):
        data = FullLiftDataset(self.root)
        task_id = data.train_ids[0]
        native = data.privileged_task(task_id, split="train")
        native.query = "modified"
        native.expected_result.content_reqs = "modified"
        self.assertNotEqual(data.task(task_id).instruction, "modified")
        self.assertNotEqual(
            data.privileged_task(task_id, split="train").expected_result.content_reqs, "modified"
        )

    def test_optional_scene_skills_are_pinned_and_public(self):
        other = Path(self.temporary.name).resolve() / "skills-data"
        full_lift_fixture(other, skills=True)
        data = FullLiftDataset(other)
        task_id = data.train_ids[0]
        self.assertIsNotNone(
            data.privileged_task(task_id, split="train").requirements.extra_skills_dir
        )
        for test_id in data.test_ids:
            self.assertIn(
                "skills/demo/SKILL.md", [a["path"] for a in data.task(test_id).attachments]
            )
            self.assertIsNotNone(
                data.privileged_task(test_id, split="test").requirements.extra_skills_dir
            )
        task = data.task(task_id)
        self.assertIn("skills/demo/SKILL.md", [a["path"] for a in task.attachments])
        workspace = Path(self.temporary.name).resolve() / "skills-staged"
        data.stage(task, workspace)
        self.assertEqual((workspace / "skills/demo/SKILL.md").read_text(), "Public upstream skill")
        self.assertNotIn(
            "GOLD_", "".join(p.read_text() for p in workspace.rglob("*") if p.is_file())
        )

    def test_revision_scope_and_roster_rejected(self):
        for key, wrong in (
            ("revision", "other"),
            ("scope", "pilot"),
            ("test_visibility", "public"),
            ("scene_counts", {}),
        ):
            previous = self.manifest[key]
            self.manifest[key] = wrong
            self.rewrite()
            with self.subTest(key=key), self.assertRaises(ValueError):
                FullLiftDataset(self.root)
            self.manifest[key] = previous

    def test_duplicate_traversal_noncanonical_and_unknown_paths_rejected(self):
        original = dict(self.manifest["files"][0])
        for path in (
            original["path"],
            "../outside.txt",
            "/absolute.txt",
            "benchmark_mds//x",
            "benchmark_mds/./x",
            "benchmark_mds/other/train/q1_x/q1_x.md",
            "benchmark_mds\\outside",
        ):
            self.manifest["files"].append({**original, "path": path})
            self.rewrite()
            with self.subTest(path=path), self.assertRaises(ValueError):
                FullLiftDataset(self.root)
            self.manifest["files"].pop()

    def test_missing_definition_or_file_and_wrong_split_rejected(self):
        row = next(r for r in self.manifest["files"] if r["path"].endswith("q6_fixture.md"))
        self.manifest["files"].remove(row)
        self.rewrite()
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)
        self.manifest["files"].append(row)
        self.rewrite()
        path = self.root / row["path"]
        path.unlink()
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)
        # Reclassifying a pinned question must fail before parsing it.
        row["path"] = row["path"].replace("/test/", "/train/")
        self.rewrite()
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)

    def test_tamper_checksum_size_and_manifest_rejected(self):
        data = FullLiftDataset(self.root)
        row = self.manifest["files"][0]
        path = self.root / row["path"]
        original = path.read_bytes()
        path.write_bytes(original + b"tamper")
        with self.assertRaises(ValueError):
            data.task(data.ids[0])
        path.write_bytes(original)
        row["size"] += 1
        self.rewrite()
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)
        with self.assertRaises(ValueError):
            data.identity()

    def test_unlisted_file_and_symlinks_rejected(self):
        data = FullLiftDataset(self.root)
        original = self.root / self.manifest["files"][0]["path"]
        extra = original.parent / "unlisted.txt"
        extra.write_text("unexpected")
        with self.assertRaises(ValueError):
            data.identity()
        extra.unlink()
        original_bytes = original.read_bytes()
        external = Path(self.temporary.name).resolve() / "external"
        external.write_bytes(original_bytes)
        original.unlink()
        original.symlink_to(external)
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)
        original.unlink()
        original.write_bytes(original_bytes)
        link = Path(self.temporary.name).resolve() / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            FullLiftDataset(link)
        manifest_path = self.root / data.manifest_name
        external.write_bytes(manifest_path.read_bytes())
        manifest_path.unlink()
        manifest_path.symlink_to(external)
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)

    def test_stage_rejects_forged_task_dirty_workspace_and_symlink(self):
        data = FullLiftDataset(self.root)
        task = data.task(data.train_ids[0])
        workspace = Path(self.temporary.name).resolve() / "workspace"
        forged = AgentTask(task.id, task.instruction + "changed", task.attachments)
        with self.assertRaises(ValueError):
            data.stage(forged, workspace)
        self.assertFalse(workspace.exists())
        workspace.mkdir()
        unrelated = workspace / "other-task.txt"
        unrelated.write_text("another task")
        with self.assertRaises(ValueError):
            data.stage(task, workspace)
        unrelated.unlink()
        link = workspace / "q1_materials"
        link.symlink_to(Path(self.temporary.name).resolve(), target_is_directory=True)
        with self.assertRaises(ValueError):
            data.stage(task, workspace)
        link.unlink()
        workspace.rmdir()
        workspace.symlink_to(Path(self.temporary.name).resolve(), target_is_directory=True)
        with self.assertRaises(ValueError):
            data.stage(task, workspace)

    def test_duplicate_question_and_ambiguous_sections_rejected(self):
        row = next(r for r in self.manifest["files"] if r["path"].endswith("q1_fixture.md"))
        other = row["path"].replace("q1_fixture", "q1_duplicate")
        self.manifest["files"].append({**row, "path": other})
        self.rewrite()
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)
        self.manifest["files"].pop()
        path = self.root / row["path"]
        path.write_text(path.read_text() + "\n### query\nsmuggled")
        row["sha256"] = sha(path)
        row["size"] = path.stat().st_size
        self.rewrite()
        with self.assertRaises(ValueError):
            FullLiftDataset(self.root)
