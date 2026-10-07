# Custom tasks

[English](../en/custom-tasks.md) · [简体中文](../zh-CN/custom-tasks.md) · [README](../../README.md)

Task integration supplies an input adapter and an independent evaluator. Save the following complete live example as a script. First supply the three connection variables in the process environment as described in [configuration](configuration.md); the SDK does not load `.env`. This example reuses the packaged maintenance task. A new domain must also replace source kinds, schema, assets, and question contracts. Use a fresh output directory.

```python
import asyncio
import time
from pathlib import Path
from darwinagent import (
    CaseInput, CorpusBlock, QuestionInput, SourceRef, EvaluationResult,
    Config, RunConfig, Pipeline, TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient

class Records:
    def generation_input(self, case_id):
        return CaseInput(case_id,
            (CorpusBlock(SourceRef("maintenance_record", case_id, "row-1"),
                         "设备 D-17 于 2026-09-01 由林维护。"),),
            (QuestionInput("q1", "谁在什么时候维护了 D-17？",
                           {"serial": "D-17"}),))

class Score:
    async def evaluate(self, result):
        correct = sum(a.status == "answered" and "林" in a.answer
                      and "2026-09-01" in a.answer for a in result.answers)
        faults = sum(a.status == "execution_error" for a in result.answers)
        return EvaluationResult({"correct": correct}, len(result.answers),
                                len(result.answers) - faults, faults, 0)

async def main():
    work = Path("runs/custom-live")
    config = Config.from_env(work_dir=work)
    config.validate_model()
    config.max_http_requests = 40
    config.request_budget_path = work / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800
    # Start with the packaged task. For another domain, register your own root.
    bundle = load_assets(TASK_ROOT).export(work / "assets")
    spec = TaskSpec.load(TASK_ROOT / "task.yaml", bundle)
    client = LLMClient(config)
    try:
        result = await Pipeline(client, work / "generation").run(
            Records().generation_input("device-custom"), spec, RunConfig())
        score = await Score().evaluate(result)
        print(result.to_dict())
        print(score.to_dict())
    finally:
        await client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
```

This example demonstrates SDK integration, without starting the optimization loop. For zero-network execution, run the complete verified offline example:

```bash
uv run python examples/third_domain.py
```

It prints `林于 2026-09-01 维护了设备 D-17。`. Model responses are explicitly recorded; the example evaluator scores the answer content.

## Register assets

A task root requires `task.yaml` and `assets/index.yaml`. Registration is declarative and does not import task execution modules. See the [packaged example](../../src/darwinagent/demo/device_maintenance/task.yaml). Assets usually occupy `assets/{S,F,C,P}/`.

| Kind | Contract |
| --- | --- |
| S | Exactly one schema asset |
| F | At least one query function; registered input/output contracts, schema dependencies, and trial inputs |
| C | Optional; graph or answer stage; consistent `{ok: bool, issues: [string]}` result |
| P | One prompt for each fixed role: extract, tools, answer, review; registered slots only |

`load_assets(root).export(path)` rejects a nonempty asset directory. `TaskSpec.load(root / "task.yaml", bundle)` binds the declaration to its version. F/C run under restricted capabilities. Evaluator references must stay out of `CaseInput`, assets, proposal training inputs, and query results. A held-out study additionally requires frozen disjoint sets and an independent evaluation protocol; see [experiments](experiments.md).
