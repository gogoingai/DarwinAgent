"""Serialize durable reservations across clients and worker processes."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from .artifacts import atomic_json


@contextmanager
def counter_transaction(path: Path):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as file:
        if os.name == "nt":
            import msvcrt
            if file.tell() == 0:
                file.write(b"0"); file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            counts = json.loads(path.read_text()) if path.exists() else {}
            yield counts
            atomic_json(path, counts)
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
