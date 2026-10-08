# Use your own data and tasks

[简体中文](../zh-CN/custom-tasks.md) · [English home](../../README.md)

Install the package and configure `.env` using the [quickstart](quickstart.md). Start by replacing the maintenance task's inputs, then register assets for a new domain.

## 1. Run your own records through the pipeline

Create `custom_task.py` beside `.env` and copy this complete script. No repository checkout is needed; it loads task declarations and assets from the installed package.

`Records` supplies source records and questions. `Pipeline` uses the model to extract, query, and answer. `Score` independently evaluates the actual answer.

```python
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import (
    CaseInput,
    Config,
    CorpusBlock,
    EvaluationResult,
    Pipeline,
    QuestionInput,
    RunConfig,
    SourceRef,
    TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient


class Records:
    def generation_input(self, case_id):
        return CaseInput(
            case_id,
            (
                CorpusBlock(
                    SourceRef("maintenance_record", case_id, "row-1"),
                    "Device D-17 was maintained by Lin on 2026-09-01.",
                ),
            ),
            (QuestionInput("q1", "Who maintained D-17 and when?", {"serial": "D-17"}),),
        )


class Score:
    async def evaluate(self, result):
        correct = sum(
            answer.status == "answered" and "Lin" in answer.answer and "2026-09-01" in answer.answer
            for answer in result.answers
        )
        faults = sum(answer.status == "execution_error" for answer in result.answers)
        return EvaluationResult(
            {"correct": correct},
            len(result.answers),
            len(result.answers) - faults,
            faults,
            0,
        )


async def main():
    load_dotenv(Path(".env"))
    output = Path("runs/custom-live").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    bundle = load_assets(TASK_ROOT).export(output / "assets")
    spec = TaskSpec.load(TASK_ROOT / "task.yaml", bundle)
    async with asyncio.timeout(1800), LLMClient(config) as client:
        result = await Pipeline(client, output / "generation").run(
            Records().generation_input("device-custom"), spec, RunConfig()
        )
    score = await Score().evaluate(result)
    report = {
        "answers": [
            {
                "question_id": answer.question_id,
                "status": answer.status,
                "answer": answer.answer,
            }
            for answer in result.answers
        ],
        "evaluation": score.to_dict(),
        "output": str(output),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

Run:

```bash
python custom_task.py
```

The script prints answers and scores and saves artifacts in `runs/custom-live/`. `metrics.correct` is `1` if the answer includes the technician and date, otherwise `0`; execution faults are counted separately. This simple string-matching evaluator illustrates the boundary. Use suitable evaluation rules for your application.

It calls your model with a limit of 40 HTTP attempts and 1800 seconds. Choose a fresh output directory for another independent run; asset export rejects a nonempty directory. The downloadable [example file](../../examples/custom_task.py) uses the same API.

## 2. What to change

| Part | Your responsibility |
| --- | --- |
| `CorpusBlock.text` | Supply the original record for the model |
| `SourceRef` | Identify its source kind, document, and fragment |
| `QuestionInput` | Supply a question ID, question text, and query parameters |
| `Score.evaluate()` | Score actual output using your references or business rules |
| `output` | Choose a separate directory for each independent run |

Within this device domain, start by replacing records and questions. The registered query parameter is `serial`, the source kind is `maintenance_record`, and records describe the device, date, and technician. Keep inputs consistent with those asset contracts. Evaluation references belong inside `Score`, outside model input.

**This script runs and scores one task.** To try the baseline → proposal → evaluation → adoption → experience loop, use the [live Python demo](quickstart.md#4-call-it-from-python). For your own improvement loop, copy the [complete custom experiment](custom-experiments.md). See [experiments](experiments.md) for evaluation and held-out protocols.

## 3. Register a new domain

A new domain needs its own declaration and S/F/C/P assets, in addition to new questions. Copy the installed maintenance task into your project:

```python
from pathlib import Path
from shutil import copytree

from darwinagent.demo import TASK_ROOT

copytree(TASK_ROOT, Path("my-task"))
```

Replace `TASK_ROOT` with `Path("my-task")` in the script above, then edit:

```text
my-task/
├── task.yaml
└── assets/
    ├── index.yaml
    ├── S/schema.yaml
    ├── F/lookup.py
    ├── C/subject.py
    └── P/
        ├── extract.txt
        ├── tools.txt
        ├── answer.txt
        └── review.txt
```

| File / asset | Responsibility |
| --- | --- |
| `task.yaml` | Task name, source kinds, query parameters, and answer format |
| `assets/index.yaml` | Asset IDs, paths, contracts, and dependencies |
| S: schema | Entity types, properties, and relations; exactly one |
| F: query functions | Query graph evidence; at least one, with input/output contracts and trial inputs |
| C: task checks | Optional graph or answer checks returning `{ok: bool, issues: [string]}` |
| P: role prompts | One each for extract, tools, answer, and review; registered slots only |

Registration is declarative and does not import task execution modules. `load_assets(root).export(path)` exports an independent asset version. `TaskSpec.load(root / "task.yaml", bundle)` binds the task to that version. Functions and checks run under restricted capabilities.

Change inputs, schema, queries, prompts, and evaluator together for a new domain. Keep evaluation references out of generation input, assets, and query results. The bundled demo contains one synthetic question; formal studies also require disjoint training, validation, and test sets.

## Repository-only offline example

For an offline interface example, check out the source and run:

```bash
uv sync --frozen
uv run python examples/third_domain.py
```

The [offline script](../../examples/third_domain.py) uses recorded responses with no model requests. Installing `darwinagent` does not install the repository's root `examples/` directory.
