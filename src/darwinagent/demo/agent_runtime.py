"""Credential-free generic Agent example: real files, independent loops and durable replay.

Run ``python -m darwinagent.demo.agent_runtime --output runs/agent-offline``.
Recorded ReAct responses validate execution mechanics, not model quality.
"""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from darwinagent import (
    AgentAsset,
    AgentKernelAssets,
    AgentKernelBundle,
    AgentTask,
    ExecutionBackend,
    ExecutionProfile,
    ReActAgent,
    ToolFailure,
)
from darwinagent.agent import file_tools


class OwnLoopAgent:
    """An ordinary adapter with its own recovery/iteration policy, independent of ReAct."""

    def identity(self):
        return {"policy": "optional-preface-then-save-v1"}

    async def run(self, task, context):
        for source in ("optional-preface.txt",):
            try:
                await context.call("read_file", {"path": source})
            except ToolFailure as failure:
                if failure.error_type != "FileNotFoundError":
                    raise
        artifacts = []
        for name in task.parameters["outputs"]:
            artifacts.append(await context.call("save_report", {"path": name}))
        return context.result("Reports saved", artifacts=artifacts)


class RecordedClient:
    """A local implementation of the chat client contract; performs no HTTP requests."""

    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        turns = sum(message["role"] == "assistant" for message in kwargs["messages"])
        action = (
            {"tool": "save_report", "params": {"path": "report.txt"}}
            if turns == 0
            else {"final_response": "Report saved", "artifacts": [{"path": "report.txt"}]}
        )
        return SimpleNamespace(content=json.dumps(action), usage={}, model="offline-recorded")


def make_kernel(root, profile, *, uppercase):
    """Keep S/P/C fixed while F changes observable file content."""
    if root.exists():
        return AgentKernelBundle(root)
    schema = {
        "nodes": [
            {
                "id": "Document",
                "source": "attachments",
                "fields": {"text": {"path": "content", "type": "string"}},
                "where": {},
            }
        ],
        "relations": [],
    }
    function = 'def run(params):\n    text = view()["nodes"][0]["fields"]["text"]\n'
    if uppercase:
        function += "    text = text.upper()\n"
    function += '    return call("write_file", {"path": params["path"], "content": text})\n'
    assets = (
        AgentAsset("schema", "S", json.dumps(schema)),
        AgentAsset(
            "save_report",
            "F",
            function,
            schema_dependencies=("schema",),
            trial_inputs=({"path": "report.txt"},),
        ),
        AgentAsset(
            "delivery",
            "C",
            'def check(candidate):\n    return bool(candidate["artifacts"])\n',
            stage="final_delivery",
            schema_dependencies=("schema",),
        ),
        AgentAsset("worker", "P", "$instruction", role="worker", schema_dependencies=("schema",)),
    )
    return AgentKernelAssets(assets, profile).export(root)


class WorkspaceBackend(ExecutionBackend):
    """Campaign factory: each allocated episode gets isolated write tools and receipts."""

    def for_episode(self, episode_root, task):
        episode_root = Path(episode_root)
        return WorkspaceBackend(
            self.adapter,
            file_tools(episode_root / "workspace"),
            self.profile,
            client=self.client,
            run_root=episode_root / "receipts",
        )


async def run_example(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile = ExecutionProfile("offline-files-v1", ("read_file", "write_file"))
    task = AgentTask(
        "report",
        "Save the supplied text as a report.",
        attachments=({"content": "auditable report"},),
        parameters={"outputs": ["report.txt"]},
    )
    kernels = [
        make_kernel(root / name, profile, uppercase=upper)
        for name, upper in (("K0", False), ("K1", True))
    ]
    assert kernels[0].get("worker").fingerprint == kernels[1].get("worker").fingerprint
    assert kernels[0].get("save_report").fingerprint != kernels[1].get("save_report").fingerprint
    results = {}
    for label, adapter in (("own-loop", OwnLoopAgent()), ("react", ReActAgent())):
        for name, kernel, expected in zip(
            ("K0", "K1"), kernels, ("auditable report", "AUDITABLE REPORT"), strict=True
        ):
            client = RecordedClient() if label == "react" else None
            backend = WorkspaceBackend(
                adapter,
                file_tools(root / "factory-workspace"),
                profile,
                client=client,
                run_root=root / "factory-receipts",
            ).for_episode(root / label / name, task)
            result = await backend.run(task, kernel, run_id="report-1")
            assert result.status == "completed", result.to_dict()
            assert (root / label / name / "workspace/report.txt").read_text() == expected
            calls = client.calls if client else 0
            resumed = await backend.run(task, kernel, run_id="report-1")
            assert resumed.to_dict() == result.to_dict()
            assert client is None or client.calls == calls
            assert result.usage["leaf_calls"] == (2 if label == "own-loop" else 1)
            results[label + "/" + name] = result.to_dict()
    summary = {"mechanism_only": True, "actual_model_http": 0, "episodes": results}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = asyncio.run(run_example(args.output))
    print(
        json.dumps(
            {
                "status": "passed",
                "episodes": len(summary["episodes"]),
                "actual_model_http": 0,
                "output": str(args.output.resolve()),
            }
        )
    )


if __name__ == "__main__":
    main()
