"""Synthetic full EALE source trees; contains no upstream task contents."""

from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json
from datasets.lift.data import EALE_REVISION, sha
from datasets.lift.full_data import SCENES


def full_lift_fixture(root, *, skills=False):
    root = Path(root)
    for scene in SCENES:
        for number in range(1, 7):
            question = f"q{number}"
            split = "train" if number <= 4 else "test"
            directory = root / "benchmark_mds" / scene / split / (question + "_fixture")
            directory.mkdir(parents=True)
            (directory / (question + "_fixture.md")).write_text(
                f"### query\nPublic query {scene} {question}\n"
                f"### 要求\nGOLD_CONTENT_{question}\n"
                f"### 轨迹要求\nGOLD_TRAJECTORY_{question}\n",
                encoding="utf-8",
            )
            # Both upstream resource layouts, plus tasks with no resources.
            if number in (1, 2, 5):
                name = "materials" if number == 2 else question + "_materials"
                material = directory / name / "nested"
                material.mkdir(parents=True)
                (material / "public.txt").write_text(f"public resource {scene} {question}")
        if skills:
            skill = root / "benchmark_mds" / scene / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("Public upstream skill")
    rows = [
        {"path": p.relative_to(root).as_posix(), "sha256": sha(p), "size": p.stat().st_size}
        for p in sorted((root / "benchmark_mds").rglob("*"))
        if p.is_file()
    ]
    manifest = {
        "revision": EALE_REVISION,
        "scope": "full_EALE",
        "test_visibility": "privileged_dataset_only",
        "scene_counts": {s: {"train": 4, "test": 2} for s in SCENES},
        "files": rows,
    }
    atomic_json(root / "full-download-manifest.json", manifest)
    return manifest
