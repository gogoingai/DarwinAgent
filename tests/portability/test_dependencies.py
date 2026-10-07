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
                any(i and i.split(".")[0] in {"datasets", "tasks", "oak_domains"} for i in imports),
                str(path),
            )

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
