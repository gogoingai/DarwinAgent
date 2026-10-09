"""Offline Agent loops, recorded transport and v4 assets shared by runtime tests."""

import asyncio
import json
from types import SimpleNamespace

import yaml

from darwinagent.agent import AgentAsset, AgentKernelAssets, ToolFailure


class ScriptedAgent:
    def identity(self):
        return {}

    async def run(self, task, context):
        artifact = await context.call("save", {"path": "report.txt"})
        return context.result("delivered", artifacts=(artifact,))


class TwoWritesAgent:
    def identity(self):
        return {}

    interrupt = False

    async def run(self, task, context):
        first = await context.call("write_file", {"path": "first.txt", "content": "first"})
        if self.interrupt:
            raise asyncio.CancelledError
        second = await context.call("write_file", {"path": "second.txt", "content": "second"})
        return context.result("done", artifacts=(first, second))


class ModelTwiceAgent:
    def identity(self):
        return {}

    interrupt = False

    async def run(self, task, context):
        first = await context.model([{"role": "user", "content": "first"}])
        if self.interrupt:
            raise asyncio.CancelledError
        second = await context.model([{"role": "user", "content": "second"}])
        return context.result(first.content + second.content)


class RecordedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, **kwargs):
        content = self.responses[self.calls]
        self.calls += 1
        return SimpleNamespace(
            content=json.dumps(content),
            usage={"prompt_tokens": 10, "completion_tokens": 3},
            model="offline",
        )


def schema(path="content"):
    return {
        "nodes": [
            {
                "id": "Document",
                "source": "attachments",
                "fields": {"text": {"path": path, "type": "string"}},
                "where": {},
            }
        ],
        "relations": [],
    }


def bundle(root, profile, *, source_path="content", uppercase=False, checks=()):
    f = 'def run(params):\n    graph = view()\n    text = graph["nodes"][0]["fields"]["text"]\n'
    if uppercase:
        f += "    text = text.upper()\n"
    f += '    return call("write_file", {"path":params["path"],"content":text})\n'
    assets = [
        AgentAsset("schema", "S", yaml.safe_dump(schema(source_path))),
        AgentAsset(
            "save", "F", f, schema_dependencies=("schema",), trial_inputs=({"path": "report.txt"},)
        ),
        AgentAsset("worker", "P", "$instruction", role="worker", schema_dependencies=("schema",)),
        *checks,
    ]
    return AgentKernelAssets(tuple(assets), profile).export(root)


class LostReceiptClient:
    calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        raise ConnectionError("dispatched but receipt lost")


class CatchesUnknownAgent:
    def identity(self):
        return {}

    async def run(self, task, context):
        try:
            await context.call("write_file", {"path": "caught.txt", "content": "written"})
        except Exception:
            pass
        return context.result("claims success")


class ReturnsUnknownAgent:
    def identity(self):
        return {}

    async def run(self, task, context):
        return context.result(status="unknown")


class ConfiguredAgent:
    def __init__(self, response):
        self.response = response

    def identity(self):
        return {"response": self.response}

    async def run(self, task, context):
        return context.result(self.response)


class RecoveringReadAgent:
    interrupt = False

    def __init__(self):
        self.failures = []

    def identity(self):
        return {"source_path": "missing.txt", "fallback": "recovered.txt"}

    async def run(self, task, context):
        try:
            await context.call("read_file", {"path": "missing.txt"})
        except ToolFailure as error:
            if error.error_type != "FileNotFoundError":
                raise
            self.failures.append(error.to_dict())
        if self.interrupt:
            raise asyncio.CancelledError
        artifact = await context.call(
            "write_file", {"path": "recovered.txt", "content": "fallback evidence"}
        )
        return context.result("recovered", artifacts=(artifact,))
