"""Restricted programs for known and uncertain macro failure scenarios."""

import yaml

from darwinagent.agent import AgentAsset, AgentKernelAssets
from tests.support.agent_runtime import schema


def macro_bundle(root, profile, code):
    return AgentKernelAssets(
        (
            AgentAsset("schema", "S", yaml.safe_dump(schema())),
            AgentAsset("combine", "F", code, trial_inputs=({},), schema_dependencies=("schema",)),
            AgentAsset(
                "worker", "P", "$instruction", role="worker", schema_dependencies=("schema",)
            ),
        ),
        profile,
    ).export(root)


OVERSIZED_AFTER_WRITE = """def run(params):
    call("write_file", {"path":"saved.txt", "content":"already done"})
    first = call("read_file", {"path":"one.txt"})
    second = call("read_file", {"path":"two.txt"})
    return {"first": first, "second": second}
"""
