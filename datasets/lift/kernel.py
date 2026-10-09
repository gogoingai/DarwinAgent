"""A fixed initial S/F/C/P bundle, shared across all kernel arms."""

import yaml

from darwinagent.agent import AgentAsset, AgentKernelAssets

from .agents import worker_profile


def initial_kernel(root):
    schema = {
        "nodes": [
            {
                "id": "Task",
                "source": "task",
                "where": {},
                "fields": {"instruction": {"path": "instruction", "type": "string"}},
            },
            {
                "id": "Material",
                "source": "attachments",
                "where": {},
                "fields": {"path": {"path": "path", "type": "string"}},
            },
        ],
        "relations": [],
    }
    return AgentKernelAssets(
        (
            AgentAsset("schema", "S", yaml.safe_dump(schema)),
            AgentAsset(
                "inspect_materials",
                "F",
                'def run(params):\n    return call("list_files", {})\n',
                schema_dependencies=("schema",),
                trial_inputs=({},),
                description="List task materials",
            ),
            AgentAsset(
                "delivery",
                "C",
                'def check(candidate):\n    return len(candidate["final_response"]) > 0\n',
                stage="final_delivery",
                schema_dependencies=("schema",),
            ),
            AgentAsset(
                "worker",
                "P",
                "$instruction\nRead the metric definitions and input tables. Compute from source values, write the requested artifact, then report its path.\n$tools\n$view",
                role="worker",
                schema_dependencies=("schema",),
            ),
        ),
        worker_profile(),
    ).export(root)
