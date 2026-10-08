# Quickstart

[English](../en/quickstart.md) · [简体中文](../zh-CN/quickstart.md) · [README](../../README.md)

The experimental 0.1.0 release is available on [PyPI](https://pypi.org/project/darwinagent/). Install it in a Python 3.11+ environment:

```bash
python -m pip install darwinagent
darwinagent --version
darwinagent doctor --output runs/doctor
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay --resume
```

`doctor` checks installation, resources, output writability, and live configuration readiness without making a request. Explicit `--check-model` adds one real model probe with a 30-second limit. `--version` prints `darwinagent 0.1.0`.

Replay executes the real `Pipeline`, `ExperimentRunner`, Wiki, admission, and publication. It uses scripted responses, seeded B0, and P-only prompt proposals. An independent evaluator scores technician/date coverage in one synthetic question: 0.5 → 1.0 → 1.0. R1 is accepted, R2 rejected on a tie, and HTTP attempts are zero. There is no held-out set or demonstrated model-quality gain.

Use another output directory for live mode:

```bash
# Create .env with your model configuration before running live mode.
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

Set `DARWINAGENT_API_KEY`, `DARWINAGENT_BASE_URL` (your provider's API root), and `DARWINAGENT_MODEL` in `.env`. The CLI reads the current directory's `.env`; shell variables take precedence. Live mode has no scripted fallback. B0 may already be correct, so two rejections can be a valid outcome. Every dispatched HTTP attempt, including retries, counts toward the cap; the timeout covers the whole run. CLI rounds range from 0 to 10.

An existing experiment directory requires `--resume` and matching mode, model, configuration, and source identity. Use a new directory after changing any identity input; do not overwrite a frozen experiment.

Python entry point:

```python
import asyncio
from pathlib import Path
from darwinagent.demo import run_demo

asyncio.run(run_demo(Path("runs/sdk-replay"), mode="replay", rounds=2))
```

Inspect `experiment.json` for frozen identity; `B0/evaluation/`, `R1/evaluation/`, and `R2/evaluation/` for independent scores; `optimization/wiki.json` for durable experience; `R*/optimization/attempt-0/proposal-call.json` for proposal inputs; `published/current.json` for the accepted version; and `demo-summary.json` for the run summary. Live mode also writes `http_attempts.json`.

Next: [configuration](configuration.md), [experiment protocols](experiments.md), and [acceptance evidence](../acceptance/2026-10-07.md).

## Install from source

For source development, run `uv sync --frozen` in the checkout and prefix CLI commands with `uv run`. Standard pip installation from that checkout is also supported:

```bash
python3 -m venv .venv-pip
# Windows: .venv-pip\Scripts\activate
source .venv-pip/bin/activate
python -m pip install .
darwinagent doctor --output runs/pip-doctor
darwinagent demo --mode replay --rounds 2 --output runs/pip-replay
```

This installs your checkout, including local changes, instead of the PyPI release.
