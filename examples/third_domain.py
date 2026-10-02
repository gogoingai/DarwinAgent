"""Offline installed-package example: a third domain without benchmark imports."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from oak.engine import BuildEngine, InferenceEngine
from oak.contracts import AnswerResult, SourceRef
from oak.kernel import Harness, KernelAssets, KernelBundle
from oak.kernel.execution import KernelRuntime
from oak.kg.graph import EntityCandidate, RelationCandidate, build_graph, node_id
from oak.runtime import atomic_json
from oak.schema.model import Schema

SCHEMA = """entity_types:
  Device:
    primary_key: [serial, revision]
    attributes: [{name: serial, dtype: string}, {name: revision, dtype: int}]
  Technician:
    primary_key: [name]
    attributes: [{name: name, dtype: string}]
relation_types:
  maintained_by: {domain: Device, range: Technician}
"""


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "schema.yaml").write_text(SCHEMA)
        atomic_json(root / "harness.json", Harness().to_dict())
        (root / "maintainers.py").write_text('''def maintainers(device_id):
    return project_properties(traverse_relations(device_id, "maintained_by"), ["name"])
''')
        (root / "answer.txt").write_text("Return only technicians connected to the requested device.")
        assets = KernelAssets(schema_path=root / "schema.yaml", harness_path=root / "harness.json",
                              function_paths=[root / "maintainers.py"],
                              prompt_paths={"answer": root / "answer.txt"})
        assets.export(root / "bundle")
        (root / "bundle").rename(root / "relocated")
        bundle = KernelBundle.load(root / "relocated")
        runtime = KernelRuntime(bundle)
        async def builder():
            entities = [EntityCandidate("Device", {"serial": "A|B=1", "revision": 2}, {}, "manual:1"),
                        EntityCandidate("Technician", {"name": "林"}, {}, "manual:2"),
                        EntityCandidate("Technician", {"name": "王"}, {}, "manual:3")]
            relations = [RelationCandidate("maintained_by", ("Device", {"serial": "A|B=1", "revision": 2}),
                                           ("Technician", {"name": "林"}))]
            return build_graph(entities, relations, bundle.schema())
        graph = await BuildEngine(builder, lambda: bundle.manifest).run()
        async def answer(question):
            rows = runtime.call("0", graph, {"device_id": node_id("Device", {"serial": "A|B=1", "revision": 2})})
            return AnswerResult("q1", "answered", "、".join(row["name"] for row in rows),
                                (SourceRef("manual", "device-record", "maintained_by:1"),))
        answers = await InferenceEngine(answer, lambda: bundle.manifest).run(["谁维护设备？"])
        assert answers[0].answer == "林"
        assert bundle.path("prompts", "answer").read_text().startswith("Return only")
        print(json.dumps({"third_domain": "device_maintenance", "passed": True,
                          "nodes": len(graph), "harness": bundle.harness().version}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
