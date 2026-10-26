"""Optional per-process admission and emergency receipt storage for local runs."""

import errno
import os
import shutil
import sys
import uuid
from pathlib import Path

from .artifacts import atomic_json


class ResourceLimit(RuntimeError):
    pass


class ResourceGuard:
    """Reserve real disk blocks; after salvage, require an explicit new run.

    Admission checks cannot protect against other processes consuming disk after
    dispatch. The reserve provides one local persistence retry, never an HTTP retry.
    """

    def __init__(
        self,
        root,
        *,
        reserve_bytes=64 * 1024**2,
        min_free_bytes=2 * 1024**3,
        max_rss_bytes=640 * 1024**2,
    ):
        self.root = Path(root)
        self.reserve_bytes = reserve_bytes
        self.min_free_bytes = min_free_bytes
        self.max_rss_bytes = max_rss_bytes
        if reserve_bytes <= 0 or min_free_bytes < 0:
            raise ValueError("Invalid resource limits")
        self.path = self.root / (".receipt-reserve-" + uuid.uuid4().hex)
        self._inode = None
        self.exhausted = False

    def declaration(self):
        return {
            "kind": "receipt-resource-guard-v1",
            "root": str(self.root.resolve()),
            "reserve_bytes": self.reserve_bytes,
            "min_free_bytes": self.min_free_bytes,
            "max_rss_bytes": self.max_rss_bytes,
        }

    def check(self):
        try:
            import resource
        except ImportError:
            raise ResourceLimit("Process memory accounting is unavailable") from None
        if self.exhausted:
            raise ResourceLimit("Emergency receipt reserve consumed; stop model dispatch")
        if self._inode is None:
            raise ResourceLimit("Receipt reserve is not prepared")
        if shutil.disk_usage(self.root).free < self.min_free_bytes:
            raise ResourceLimit("Insufficient disk space for model dispatch")
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss *= 1 if sys.platform == "darwin" else 1024
        if self.max_rss_bytes is not None and rss > self.max_rss_bytes:
            raise ResourceLimit("Process memory exceeds model dispatch limit")

    def prepare(self):
        if self.exhausted or self._inode is not None:
            raise ResourceLimit("Receipt reserve cannot be reset")
        self.root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < self.min_free_bytes + self.reserve_bytes:
            raise ResourceLimit("Insufficient space to prepare receipt reserve")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._inode = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        try:
            with os.fdopen(fd, "wb") as stream:
                block = bytes(min(self.reserve_bytes, 1024**2))
                remaining = self.reserve_bytes
                while remaining:
                    size = min(remaining, len(block))
                    stream.write(block[:size])
                    remaining -= size
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            self.close()
            raise
        try:
            self.check()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._inode is None:
            return
        stat = self.path.lstat()
        if (stat.st_dev, stat.st_ino) != self._inode:
            raise ResourceLimit("Receipt reserve identity changed")
        self.path.unlink()
        self._inode = None

    def persist_json(self, path, value, *, writer=atomic_json):
        try:
            writer(path, value)
        except OSError as exc:
            if exc.errno != errno.ENOSPC or self.exhausted or self._inode is None:
                raise
            self.exhausted = True
            self.close()
            writer(path, value)
