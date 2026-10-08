import ast
import unittest

from tests.support.device import ROOT


class DependencyBoundaries(unittest.TestCase):
    def test_core_does_not_import_tasks_or_datasets(self):
        for path in (ROOT / "src/darwinagent").rglob("*.py"):
            tree = ast.parse(path.read_text())
            imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            imports.extend(
                a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
            )
            self.assertFalse(
                any(
                    i and i.split(".")[0] in {"datasets", "tasks", "tests", "mem0", "oak_domains"}
                    for i in imports
                ),
                str(path),
            )

    def test_experiment_helpers_do_not_import_runner(self):
        helpers = {
            "lifecycle",
            "stages",
            "optimization",
            "rounds",
            "graph_trials",
            "constants",
            "feedback",
            "recovery",
            "trials",
            "statistics",
            "snapshots",
            "wiki_evidence",
            "wiki_lessons",
            "wiki_context",
            "admission_samples",
            "admission_checks",
            "admission_functions",
            "admission_composition",
            "admission_reporting",
        }
        for name in helpers:
            path = ROOT / "src/darwinagent/experiments" / (name + ".py")
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or "", *[a.name for a in node.names]]
                else:
                    continue
                self.assertFalse(any("runner" in m.split(".") for m in modules), str(path))

    def test_package_import_has_no_configuration_client_or_directory_side_effect(self):
        import subprocess
        import sys
        import tempfile

        code = """
import openai, networkx, yaml, dotenv, httpx
from pathlib import Path
from unittest.mock import patch
with patch('os.getenv', side_effect=AssertionError('environment read')), \\
     patch('os.environ.get', side_effect=AssertionError('environment read')), \\
     patch.object(Path, 'mkdir', side_effect=AssertionError('directory creation')), \\
     patch.object(openai, 'AsyncOpenAI', side_effect=AssertionError('model client')), \\
     patch.object(openai, 'OpenAI', side_effect=AssertionError('model client')):
    import darwinagent
    assert darwinagent.__version__ == '0.1.2'
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agents_invoke_assets_through_runtime(self):
        for path in (ROOT / "src/darwinagent/agents").glob("*.py"):
            text = path.read_text()
            self.assertNotIn("from darwinagent.operators", text)
            self.assertNotIn("exec(", text)
        self.assertFalse((ROOT / "src/darwinagent/kernel/harness.py").exists())
        self.assertFalse(any((ROOT / "oak_domains").rglob("*.py")))

    def test_no_dataset_flow_classes(self):
        for name in ["locomo", "travelplanner"]:
            for filename in ["adapter.py", "evaluator.py", "exports.py", "run.py"]:
                path = ROOT / "datasets" / name / filename
                tree = ast.parse(path.read_text())
                self.assertFalse(
                    any(
                        isinstance(n, ast.ClassDef) and n.name.endswith(("Agent", "Pipeline"))
                        for n in ast.walk(tree)
                    )
                )

    def test_wheel_contains_core_only(self):
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"], ["src/darwinagent"]
        )
