"""Local boundary tests for the exact container toolbox implementation."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datasets.lift import toolbox


class ToolboxTests(unittest.TestCase):
    def test_workspace_sql_and_writes(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(toolbox, "ROOT", Path(temporary)),
        ):
            toolbox.execute(
                "write_text", {"path": "data.csv", "content": "name,amount\na,2\na,3\nb,7\n"}
            )
            result = toolbox.execute(
                "transform_tables",
                {
                    "tables": {"sales": "data.csv"},
                    "sql": "SELECT name,SUM(CAST(amount AS REAL)) AS total FROM sales GROUP BY name ORDER BY total DESC",
                },
            )
            self.assertEqual(
                result["rows"], [{"name": "b", "total": 7.0}, {"name": "a", "total": 5.0}]
            )
            with self.assertRaises(ValueError):
                toolbox.execute("read_text", {"path": "../private.txt"})
            with self.assertRaises(Exception):
                toolbox.execute(
                    "transform_tables",
                    {"tables": {"sales": "data.csv"}, "sql": "DELETE FROM sales"},
                )
            outside = Path(temporary).parent / "outside-lift-sentinel"
            (Path(temporary) / "link").symlink_to(outside)
            with self.assertRaises(ValueError):
                toolbox.execute("write_text", {"path": "link", "content": "blocked"})

    def test_connections_close_on_success_and_denied_sql(self):
        closed = []
        original = sqlite3.connect

        class Connection(sqlite3.Connection):
            def close(self):
                closed.append(True)
                super().close()

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(toolbox, "ROOT", Path(directory)),
        ):
            (Path(directory) / "data.csv").write_text("name,amount\na,2\n")
            with patch.object(
                toolbox.sqlite3, "connect", side_effect=lambda *a: original(*a, factory=Connection)
            ):
                toolbox.execute(
                    "transform_tables",
                    {"tables": {"sales": "data.csv"}, "sql": "SELECT * FROM sales"},
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    toolbox.execute(
                        "transform_tables",
                        {"tables": {"sales": "data.csv"}, "sql": "DELETE FROM sales"},
                    )
            self.assertEqual(len(closed), 2)

    def test_unknown_capability_is_rejected(self):
        with self.assertRaises(ValueError):
            toolbox.execute("shell", {"command": "pwd"})
