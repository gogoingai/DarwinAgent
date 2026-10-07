"""Offline regression scenarios for conversation dates."""

import unittest

from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.dates import resolve_relative
from tests.support.graphs import graph_result_with_vector


class DatesKernel(unittest.TestCase):
    def test_relative_resolution(self):
        from datetime import date

        anchor = date(2024, 5, 8)
        self.assertEqual(resolve_relative(anchor, "昨天")[0], "2024-05-07")
        self.assertEqual(resolve_relative(anchor, "去年"), ("2023", "年"))
        self.assertEqual(resolve_relative(anchor, "上周日")[0], "2024-05-05")
        resolved, _ = resolve_relative(anchor, "3个月前")
        self.assertTrue(resolved.startswith("2024-02"))
        self.assertEqual(resolve_relative(anchor, "说不清"), ("", "无"))

    def test_capability_wrapper(self):
        caps = DataCapabilities(graph_result_with_vector())
        out = caps.relative_date("2024-05-08", "上周日")
        self.assertEqual(out["resolved"], "2024-05-05")
        with self.assertRaises(ValueError):
            caps.relative_date("not-a-date", "昨天")
