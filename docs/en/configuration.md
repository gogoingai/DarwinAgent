# Model configuration

[简体中文](../zh-CN/configuration.md) · [English home](../../README.md)

Live execution requires an API base URL, a model name, and an API key. All roles share one OpenAI-compatible Chat Completions endpoint by default. Offline replay needs no configuration.

## CLI: create .env

Create `.env` in the directory where you run commands:

```dotenv
DARWINAGENT_BASE_URL=https://your-provider.example/v1
DARWINAGENT_MODEL=your-model-name
DARWINAGENT_API_KEY=your-api-key
```

Replace these placeholders with your provider's configuration:

| Variable | Value |
| --- | --- |
| `DARWINAGENT_BASE_URL` | OpenAI-compatible API base URL, often ending in `/v1`, without `/chat/completions` |
| `DARWINAGENT_MODEL` | Exact model name supported by the endpoint |
| `DARWINAGENT_API_KEY` | Endpoint credentials |

The framework uses Chat Completions with streaming responses. Your endpoint must support that request format; services exposing only other API formats cannot be used directly.

The CLI reads `.env` only from the **current working directory**, not automatically from the script or repository directory. Existing environment variables take precedence. Clear or update stale variables if they conflict with the file. Keep credentials out of version control.

Check required fields:

```bash
darwinagent doctor --output runs/doctor
```

Check the connection:

```bash
darwinagent doctor --check-model --output runs/doctor
```

The first is offline. The second sends one real model request with a 30-second limit. Complete fields do not prove valid credentials, a supported model, or network access. See [troubleshooting](quickstart.md#7-troubleshooting).

## Python: load configuration explicitly

The SDK does not automatically read `.env`. Load it before creating and validating configuration:

```python
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import Config

load_dotenv(Path(".env"))
config = Config.from_env(work_dir=Path("runs/my-project").resolve())
config.validate_model()
```

Applications already managing credentials through environment variables do not need to load a file. You can also construct `Config(api_base_url=..., api_key=..., model_strong=..., work_dir=...)` explicitly. Tiers without a separate model use the main model.

`Config` controls model connections. `RunConfig` controls task execution rules. Neither is an optimizable task asset. See the [complete Python example](quickstart.md#4-call-it-from-python).

## Request and time limits

For the CLI demo:

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

The cap counts dispatched HTTP attempts, including retries. A task typically needs multiple model calls. `--timeout` bounds the whole demo. Request counts are not token counts or monetary costs.

For the SDK:

```python
import time

config.max_retries = 2
config.max_http_requests = 40
config.request_budget_path = config.work_dir / "http_attempts.json"
config.deadline_monotonic = time.monotonic() + 1800
config.request_timeout_s = 120
```

Share `request_budget_path` across clients for combined counting. `deadline_monotonic` is the absolute deadline for requests, including queues, streaming, and retries; `request_timeout_s` also bounds transport waits. Wrap the whole task in `asyncio.timeout(...)` as in the [complete example](quickstart.md#4-call-it-from-python) to cover processing outside model requests too.

## Advanced: separate models by role

Start with the default single model. To override extraction and tool roles in the SDK:

```python
import os

config.model_fast = "your-fast-model"
config.fast_base_url = "https://fast-provider.example/v1"
config.fast_api_key = os.environ["OPTIONAL_FAST_API_KEY"]
config.role_tiers = {**config.role_tiers, "extraction": "fast", "tools": "fast"}
```

`OPTIONAL_FAST_API_KEY` is read by this snippet, not an automatic CLI setting. The middle tier supports `model_middle`, `middle_base_url`, and `middle_api_key`. Other roles retain the main connection.

Provider-specific parameters are disabled by default. Enable them with CLI `--model-profiles` or `DARWINAGENT_MODEL_PROFILES=true`, or SDK `config.model_profiles = True`, only when your endpoint supports the parameters. `Config.from_env()` loads the three connection variables, not this switch.

Use a fresh directory when comparing models or routes. See [continuation](quickstart.md#6-inspect-results-and-resume) for daily resume and strict comparison.
