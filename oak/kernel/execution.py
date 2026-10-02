"""Execute explicitly selected, controlled single-function assets after relocation."""
from __future__ import annotations

from oak.operators.sandbox import exec_function_source, _run_with_timeout
from . import KernelBundle


class KernelRuntime:
    def __init__(self, bundle: KernelBundle):
        bundle.verify()
        self.bundle = bundle
        self.functions = {}
        for asset in bundle.manifest["assets"]:
            if asset["kind"] == "functions":
                path = bundle.path("functions", asset["id"])
                if path.suffix != ".py":
                    raise ValueError("Controlled function assets must be Python sources")
                _, function = exec_function_source(path.read_text())
                self.functions[asset["id"]] = function

    def call(self, asset_id, graph, arguments, *, timeout=2.0):
        self.bundle.verify()
        if asset_id not in self.functions:
            raise ValueError(f"Function is not published: {asset_id}")
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
        return _run_with_timeout(self.functions[asset_id], arguments, timeout, graph)
