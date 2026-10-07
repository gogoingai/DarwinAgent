"""Static dependency rules for reusable fixture code and scenario modules."""

import ast


def dependency_violations(source, *, support=False):
    """Find test-module imports, including local and literal dynamic imports.

    Support is intentionally independent of unittest. Scenarios may depend on
    support and production code; importing another scenario silently duplicates
    discovery and encourages borrowing TestCase instances as fixture builders.
    """
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    aliases[alias.asname or "importlib"] = "importlib"
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    aliases[alias.asname or alias.name] = "importlib.import_module"
    modules = []
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            modules.append((node.lineno, base))
            modules.extend((node.lineno, base + "." + alias.name) for alias in node.names)
        elif isinstance(node, ast.Call):
            function = ast.unparse(node.func)
            head, separator, tail = function.partition(".")
            function = aliases.get(head, head) + (separator + tail if separator else "")
            if function in ("__import__", "importlib.import_module", "import_module"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    modules.append((node.lineno, str(node.args[0].value)))
        if support and isinstance(node, ast.ClassDef):
            if any(ast.unparse(base).endswith("TestCase") for base in node.bases):
                violations.append((node.lineno, "TestCase fixture class"))
    for line, module in modules:
        parts = module.split(".")
        if any(part.startswith("test_") for part in parts) or module == "tests.fixtures":
            violations.append((line, "scenario dependency: " + module))
        if support and (module == "unittest" or module.startswith("unittest.")):
            violations.append((line, "unittest dependency: " + module))
    return sorted(set(violations))
