"""A fixed initial S/F/C/P bundle, shared across all kernel arms."""

import yaml

from darwinagent.agent import AgentAsset, AgentKernelAssets

from .agents import worker_profile


def initial_kernel(root, *, max_model_calls=8):
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
                "def run(params):\n"
                '    paths = params.get("paths", [])\n'
                "    if not paths:\n"
                "        paths = []\n"
                '        for node in view()["nodes"]:\n'
                '            if node["type"] == "Material":\n'
                '                paths.append(node["fields"]["path"])\n'
                "    loaded = []\n"
                "    skipped = []\n"
                "    for path in paths[:16]:\n"
                "        lower = path.lower()\n"
                '        if lower.endswith(".csv") or lower.endswith(".xlsx"):\n'
                '            result = call("read_table", {"path":path})\n'
                '        elif lower.endswith(".txt") or lower.endswith(".md"):\n'
                '            result = call("read_text", {"path":path})\n'
                "        else:\n"
                "            skipped.append(path)\n"
                "            continue\n"
                '        loaded.append({"path":path,"result":result})\n'
                '    return {"loaded":loaded,"skipped":skipped,"remaining_paths":paths[16:]}\n',
                schema_dependencies=("schema",),
                trial_inputs=({},),
                input_contract={
                    "type": "object",
                    "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                    "additionalProperties": True,
                },
                description="Read up to 16 public Material graph text/table paths together; optional paths overrides the graph; every read is a counted primitive",
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
        worker_profile(max_model_calls),
    ).export(root)
