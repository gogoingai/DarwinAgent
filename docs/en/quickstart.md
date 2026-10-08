# Quickstart

[简体中文](../zh-CN/quickstart.md) · [English home](../../README.md)

Start from an empty directory, configure a real model, and use either the CLI or Python API. Without model credentials, jump to [offline replay](#5-try-offline-replay-without-a-model).

## 1. Install

Use Python 3.11 or newer. Create a project and virtual environment:

```bash
mkdir darwinagent-starter
cd darwinagent-starter
```

Activate it on macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Or in Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

Install one package for both the CLI and Python API:

```bash
python -m pip install darwinagent
darwinagent --version
```

The current release is `0.1.0`. Run the remaining commands from this project directory with the environment activated.

## 2. Configure your model

Create `.env` inside `darwinagent-starter/`, alongside `.venv`:

```dotenv
DARWINAGENT_BASE_URL=https://your-provider.example/v1
DARWINAGENT_MODEL=your-model-name
DARWINAGENT_API_KEY=your-api-key
```

Replace all three placeholders. Use the API base URL from your provider's documentation, an exact model name supported by that endpoint, and your own key. Supply an OpenAI-compatible base URL without `/chat/completions`; the client adds the request path. All roles share this one model by default.

The CLI reads `.env` from the current directory; existing environment variables take precedence. Keep `.env` out of version control. The Python example below loads it explicitly. See [configuration](configuration.md).

First check installation and required configuration fields without a model request:

```bash
darwinagent doctor --output runs/doctor
```

`Live model configuration: ready` means the fields are present, not that the connection has been tested. To check the actual endpoint, key, and model:

```bash
darwinagent doctor --check-model --output runs/doctor
```

This sends one real model request with a 30-second limit. Success reports `Model endpoint OK`.

## 3. Run the complete loop from the CLI

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

The bundled task asks who maintained device D-17 and when. Its sample records are in Chinese. The model extracts a record, queries evidence, and generates an answer. An independent evaluator checks the technician and date. The framework runs a baseline, proposes prompt changes, evaluates two candidates, and saves decisions and experience.

| Argument | Meaning |
| --- | --- |
| `--mode live` | Call your configured model |
| `--rounds 2` | Run up to two candidate rounds; the CLI supports 0–10 |
| `--output runs/demo-live` | Save results in this directory |
| `--max-requests 40` | Dispatch at most 40 HTTP attempts, including retries |
| `--timeout 1800` | Limit the entire run to 1800 seconds |

On completion, the terminal includes `status: "complete"` and round `decisions`. A real model may already answer the baseline correctly; ties are rejected and scores need not rise. This tiny demo has no validation or test set.

## 4. Call it from Python

Create `main.py` in the same directory and copy this **complete script**. It uses the installed package and the same `.env`; no repository checkout is required:

```python
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import Config
from darwinagent.demo import run_demo


async def main():
    load_dotenv(Path(".env"))
    output = Path("runs/python-live").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    async with asyncio.timeout(1800):
        summary = await run_demo(output, mode="live", rounds=2, config=config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Results: {output}")


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
python main.py
```

Results go to `runs/python-live/`, separate from the CLI run. The script explicitly calls `load_dotenv()`; `Config.from_env()` only reads process environment variables. Pass `config` to `run_demo()` in live mode.

This runs the bundled demo. Continue to [custom tasks](custom-tasks.md) for your own data. The downloadable [example source](../../examples/live_demo.py) uses the same logic.

To rerun the script using saved progress, add `resume=True` to `run_demo()`. For a separate experiment, change `output` to a new directory.

## 5. Try offline replay without a model

No `.env` is needed and no model requests are sent:

```bash
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
```

Expect `status: "complete"`, round `accepted` values of `true` then `false`, and `http_attempts: 0`. The first candidate is accepted and the second rejected on a tie. The independent evaluator scores two requested fields in one synthetic question: 0.5 → 1.0 → 1.0.

Replay uses scripted model responses, a seeded baseline, and prompt-only proposals through the actual runner and evaluator. It checks mechanics and does not establish model improvement.

The Python replay also needs no configuration:

```python
import asyncio
from pathlib import Path

from darwinagent.demo import run_demo

summary = asyncio.run(run_demo(Path("runs/python-replay"), mode="replay", rounds=2))
print(summary)
```

## 6. Inspect results and resume

Start with `demo-summary.json` for status, scores, decisions, and reasons. For details, inspect:

| Path relative to the output directory | Contents |
| --- | --- |
| `experiment.json` | Initial experiment declaration |
| `B0/evaluation/maintenance-demo.json` | Baseline score |
| `R1/evaluation/maintenance-demo.json` | First candidate score; the second is in `R2` |
| `R1/optimization/attempt-0/proposal-call.json` | Proposal input and output |
| `optimization/wiki.json` | Saved experience and decisions |
| `published/current.json` | Retained asset version |
| `http_attempts.json` | Actual HTTP attempts in live mode |

Resume the CLI example:

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800 --resume
```

`--rounds` sets the total target, not the number of extra rounds. If two rounds are complete, resuming with two adds none; use `--rounds 3` to continue with a third round.

Start by resuming with the same mode and configuration. Daily continuation can record changed models and execution conditions. Add `--strict-comparison` to require the original comparison conditions. Use a new directory for a different task, dataset, or separate comparison. See [durable continuation](../workspace-continuation.md).

## 7. Troubleshooting

| Problem | What to check |
| --- | --- |
| `darwinagent` command not found | Activate the installation environment and run `python -m pip show darwinagent` |
| Missing model configuration | Check the current directory's `.env` and all three values; Python must load it explicitly |
| `doctor` passes but live mode fails | The default check is offline; use `--check-model` to test the connection |
| 401 / 403 | Check credentials and endpoint permissions |
| 404 / model not found | Check the base URL for a duplicated request path and verify the exact model name |
| Request cap or timeout reached | Inspect attempts and errors, address connection issues, adjust limits if needed, and resume |
| Output directory already exists | Use `--resume`, or choose a fresh directory |
| Every candidate is rejected | Inspect scores and reasons; rejecting ties is expected |

## Develop from source

Package users can install from PyPI. Contributors who need repository examples, tests, and development tools can run:

```bash
git clone https://github.com/gogoingai/DarwinAgent.git
cd DarwinAgent
uv sync --frozen
uv run python examples/third_domain.py
```

`examples/third_domain.py` is a repository-only offline example; it is not included in the core package. See [contributing](../../CONTRIBUTING.md) for development checks.
