"""Offline regression scenarios for recovery."""

import tempfile
import unittest
from pathlib import Path


class MaxRetriesDefault(unittest.TestCase):
    def test_transport_retries_now_ten(self):
        from darwinagent.config import Config

        cfg = Config(api_key="x", work_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(cfg.max_retries, 10)
