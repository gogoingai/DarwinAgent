"""Repeated historical checks remain cheap without hiding newly unknown tools."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support.disposition_scope import disposition_scope


class DispositionScopeCacheTests(unittest.TestCase):
    def test_unchanged_unrelated_tool_is_not_decoded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, step, receipt, unrelated = disposition_scope(Path(tmp))
            store._local_scope(step, receipt)
            original = Path.read_text
            reads = []

            def observed(path, *args, **kwargs):
                if path == unrelated:
                    reads.append(path)
                return original(path, *args, **kwargs)

            with patch.object(Path, "read_text", observed):
                store._local_scope(step, receipt)
            self.assertEqual(reads, [])

    def test_same_size_change_with_restored_mtime_is_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, step, receipt, unrelated = disposition_scope(Path(tmp))
            store._local_scope(step, receipt)
            previous = unrelated.stat()
            unrelated.write_text(unrelated.read_text().replace('"completed"', '"submitted"'))
            self.assertEqual(previous.st_size, unrelated.stat().st_size)
            os.utime(unrelated, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            with self.assertRaisesRegex(ValueError, "Unknown tool"):
                store._local_scope(step, receipt)

    def test_new_unknown_tool_and_symlink_remain_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, step, receipt, unrelated = disposition_scope(Path(tmp))
            store._local_scope(step, receipt)
            new = unrelated.with_name("new.json")
            new.write_text(json.dumps({"kind": "tool", "state": "submitted"}))
            with self.assertRaisesRegex(ValueError, "Unknown tool"):
                store._local_scope(step, receipt)
            new.unlink()
            new.symlink_to(unrelated)
            with self.assertRaisesRegex(ValueError, "symlink"):
                store._local_scope(step, receipt)
