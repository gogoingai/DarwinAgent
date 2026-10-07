"""Guard the separation between offline test scenarios and reusable fixtures."""

import importlib
import inspect
import unittest
from pathlib import Path

from tests.support.dependencies import dependency_violations

TESTS = Path(__file__).resolve().parents[1]


class SupportDependencyTests(unittest.TestCase):
    def test_support_has_no_testcase_or_scenario_dependency(self):
        for path in sorted((TESTS / "support").glob("*.py")):
            with self.subTest(path=path.name):
                self.assertEqual(dependency_violations(path.read_text(), support=True), [])
                module = importlib.import_module("tests.support." + path.stem)
                self.assertEqual(
                    [
                        name
                        for name, value in vars(module).items()
                        if inspect.isclass(value) and issubclass(value, unittest.TestCase)
                    ],
                    [],
                )

    def test_scenarios_do_not_import_other_scenarios_or_legacy_gateway(self):
        for path in sorted(TESTS.rglob("test_*.py")):
            if "fixtures" in path.parts:
                continue
            with self.subTest(path=str(path.relative_to(TESTS))):
                self.assertEqual(dependency_violations(path.read_text()), [])

    def test_guard_detects_local_dynamic_and_aliased_dependencies(self):
        cases = (
            "def build():\n    from tests.integration.test_campaign import RecordedCampaign",
            "import tests.unit.test_fact_memory as fixtures",
            "from tests.integration import test_experiment",
            "from .test_experiment import RecordedExperiment",
            "importlib.import_module('tests.integration.test_campaign')",
            "__import__('tests.unit.test_fact_memory')",
            "from unittest import TestCase as Fixture\nclass Builder(Fixture): pass",
            "import unittest as u\nclass Builder(u.TestCase): pass",
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(dependency_violations(source, support=True))
        self.assertEqual(
            dependency_violations(
                "from tests.support.device import case\nfrom darwinagent.kernel import TaskSpec",
                support=True,
            ),
            [],
        )
