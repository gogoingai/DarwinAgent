from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_files(root: Path, expected: dict[str, str]) -> None:
    changed = [name for name, fp in expected.items()
               if not (root / name).is_file()
               or hashlib.sha256((root / name).read_bytes()).hexdigest() != fp]
    if changed:
        raise ValueError(f"Frozen files changed: {changed}")
