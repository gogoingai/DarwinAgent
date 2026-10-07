<p align="center"><img src="docs/assets/hero-en.svg" alt="DarwinAgent — Evolution for the Agent Era" width="100%"></p>

# DarwinAgent

**An Open Framework for Experience-Driven Recursive Self-Improvement**

[English](README.md) · [简体中文](README.zh-CN.md)

[![Offline framework checks](https://github.com/gogoingai/DarwinAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/gogoingai/DarwinAgent/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.1.0%20experimental-45635c)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-45635c)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-45635c)](LICENSE)

DarwinAgent takes its name from Charles Darwin and the theory of evolution. **Evolution for the Agent Era** is its mission: help agents adapt through experience and retain effective capabilities. In this framework, variation comes from proposed task assets, selection comes from independent evaluation and fixed admission rules, and retention comes from versioned assets and a persistent experience Wiki that informs subsequent proposals.

DarwinAgent **0.1.0 is experimental**. It implements an inspectable improvement loop around a shared graph-based agent runtime. Improvement is an outcome to measure; a candidate can be rejected. The current scope is task assets, with fixed framework execution and evaluation boundaries.

## Why DarwinAgent

A useful answer is one run. A useful capability should survive the next run. DarwinAgent records what a candidate changed, which sources supported an answer, how it was evaluated, why it was accepted or rejected, and what experience reaches the next proposal. This makes adaptation a reproducible experiment rather than an untracked prompt edit.

## The evolution loop

![Evolution loop](docs/assets/evolution-loop-en.svg)

1. **Run:** execute a baseline through the common `Pipeline` and score its actual outputs.
2. **Vary:** propose bounded changes to S/F/C/P assets using training evidence and Wiki feedback.
3. **Select:** validate contracts and capabilities, run the candidate, and apply the frozen adoption policy.
4. **Retain:** publish accepted versions atomically; preserve rejection facts and feed experience into the next proposal.

The kernel, evaluator, permissions, and adoption rules stay outside the proposal boundary. DarwinAgent does not train model weights or rewrite its own optimizer. Its name describes the inspiration, not a claim to implement a genetic algorithm.

## What is implemented

| Capability | v0.1.0 behavior |
| --- | --- |
| Shared task runtime | `ExtractionAgent` → attributed graph → `AnswerAgent`, through one `Pipeline` |
| Bounded task assets | **S** schemas, **F** query functions, **C** task checks, **P** role prompts |
| Experience | Wiki stores observations and accepted/rejected decisions for subsequent proposals |
| Reproducibility | Content identities, separate provenance, scoped resume and optional strict comparison |
| Evaluation | Separate `DatasetAdapter` and `Evaluator`; metric names supplied by the task |
| Experiment control | `ExperimentRunner`; separate `CampaignController` for train/validation/test protocols |
| Model connection | One explicit OpenAI-compatible Chat Completions endpoint by default; optional tier overrides |
| Entry points | Installed CLI, Python SDK, offline replay, live mode, maintenance task example |

## Try the loop

Install **from this source checkout**. DarwinAgent 0.1.0 is available on `main` as an experimental source version; it has not been published to PyPI or as a GitHub Release.

```bash
# Run from this source checkout; Python 3.11+ and uv are required.
uv sync --frozen
uv run darwinagent --version
uv run darwinagent doctor --output runs/doctor
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
```

Expected replay: `status=complete`, first candidate accepted, second rejected for `primary_not_strictly_improved`, and `http_attempts=0`. The independent evaluator scores requested technician/date fields **0.5 → 1.0 → 1.0** in one tiny synthetic task. Replay uses scripted model responses, a seeded B0, and **P-only** proposals through the real controller and runtime. It demonstrates mechanics, not model learning or benchmark gains; it has no held-out evaluation.

The output directory contains:

```text
runs/demo-replay/
├── experiment.json                           # original experiment declaration
├── B0/evaluation/maintenance-demo.json         # baseline score
├── R1/evaluation/maintenance-demo.json         # candidate score (also R2)
├── R1/optimization/attempt-0/proposal-call.json # proposal input (also R2)
├── optimization/wiki.json                     # durable experience
├── published/current.json                     # accepted asset version
└── demo-summary.json                          # controller summary
```

For live execution, configure your endpoint and use a separate output directory:

```bash
cp .env.example .env
# Set DARWINAGENT_API_KEY, DARWINAGENT_BASE_URL, DARWINAGENT_MODEL in .env.
uv run darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

The CLI reads the current directory's `.env` without overriding shell variables. Live uses the actual model, with no recorded fallback. The cap counts dispatched HTTP attempts, including retries; the timeout covers the whole run. A live baseline may already be correct, so ties are rejected. Success means the stages execute and decisions are recorded, not that the score must improve.

Daily continuation retains saved work across changes to models and execution controls. Source changes take effect after a safe restart; use `--strict-comparison` when requiring the original frozen comparison conditions:

```bash
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay --resume
```

Or call the demo from Python:

```python
import asyncio
from pathlib import Path
from darwinagent.demo import run_demo

asyncio.run(run_demo(Path("runs/python-replay"), mode="replay", rounds=2))
```

[Full quickstart](docs/en/quickstart.md) · [Configuration](docs/en/configuration.md) · [Dated acceptance evidence](docs/acceptance/2026-10-07.md)

## Bring your own task

Implement the generation boundary and an independent evaluator, then register task assets in `task.yaml` and `assets/index.yaml`. The generation input carries records, questions, and source references; evaluator references stay in the evaluator.

```python
from darwinagent import (
    CaseInput, CorpusBlock, QuestionInput, SourceRef, EvaluationResult,
)

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
```

Load the actual packaged declaration and asset registry:

```python
from darwinagent import TaskSpec
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets

assets = load_assets(TASK_ROOT)  # task.yaml + assets/index.yaml + S/F/C/P files
spec = TaskSpec.load(TASK_ROOT / "task.yaml")
```

These classes plug into the common runtime; they do not replace the agent execution flow. The [complete offline example](examples/third_domain.py) runs both interfaces with registered S/F/C/P assets:

```bash
uv run python examples/third_domain.py
```

See the [custom task guide](docs/en/custom-tasks.md) for a complete live `Pipeline` example and asset contracts. Arbitrary external agent plugins are a future extension, not a v0.1 capability.

## Architecture and documentation

![Architecture](docs/assets/architecture-en.svg)

| Guide | English | 简体中文 |
| --- | --- | --- |
| Quickstart | [Read](docs/en/quickstart.md) | [阅读](docs/zh-CN/quickstart.md) |
| Architecture | [Read](docs/en/architecture.md) | [阅读](docs/zh-CN/architecture.md) |
| Configuration | [Read](docs/en/configuration.md) | [阅读](docs/zh-CN/configuration.md) |
| Custom tasks | [Read](docs/en/custom-tasks.md) | [阅读](docs/zh-CN/custom-tasks.md) |
| Experiments | [Read](docs/en/experiments.md) | [阅读](docs/zh-CN/experiments.md) |
| Migration | [Read](docs/en/migration.md) | [阅读](docs/zh-CN/migration.md) |

[Historical evidence index](docs/history/README.md) · [Editable graphics and PNG exports](docs/assets/README.md) · [Project introduction](docs/launch/introduction.en.md)

## Status and direction

The local Python 3.11/3.12/3.13 suites and installed-wheel acceptance are documented in the [dated report](docs/acceptance/2026-10-07.md). The workflow badge shows the latest GitHub CI status. Historical dataset scores belong to their original protocols and source revisions.

Next directions are broader independent task examples, controlled held-out studies, and a carefully specified external agent integration boundary. These are research and engineering plans, not shipped capabilities or promised quality gains.

## Research provenance and community

The S/F kernel is inspired by [*Toward Effective and Reliable LLM Agents via Dynamic Ontology*](https://arxiv.org/abs/2608.22974) (OaK). C/P assets and the experience Wiki are engineering extensions in this project. Historical reproduction records remain separately indexed; no paper performance numbers are used as current DarwinAgent results.

[Contributing](CONTRIBUTING.md) · [中文贡献指南](CONTRIBUTING.zh-CN.md) · [Changelog](CHANGELOG.md) · [Software citation](CITATION.cff)

MIT © 2026 DarwinAgent contributors. See [LICENSE](LICENSE); third-party material retains its own license and provenance.

## Durable continuation and Wiki queries

The Workspace APIs and offline CLI retain original evidence, request receipts, human selection history and scoped previews. Wiki queries can return raw evidence or create a resumable regroup task. See [the continuation guide](docs/workspace-continuation.md). Real model smoke remains pending explicit model selection and execution; these interfaces do not establish benchmark gains.
