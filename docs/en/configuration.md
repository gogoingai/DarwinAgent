# Configuration

[English](../en/configuration.md) · [简体中文](../zh-CN/configuration.md) · [README](../../README.md)

The default connection uses one OpenAI-compatible Chat Completions endpoint and the same model for every role/tier. No vendor URL or model is selected automatically. Live mode requires:

| Environment variable | Meaning |
| --- | --- |
| `DARWINAGENT_API_KEY` | Endpoint credential |
| `DARWINAGENT_BASE_URL` | Provider API root |
| `DARWINAGENT_MODEL` | A model available at that endpoint |
| `DARWINAGENT_MODEL_PROFILES` | Optional CLI provider parameters; false by default |

The CLI loads the current directory's `.env` without overriding shell variables. Importing the SDK does not read `.env`, create clients, or write files. SDK users must supply connection variables in the process environment before constructing configuration:

```python
import time
from pathlib import Path
from darwinagent import Config, RunConfig

config = Config.from_env(work_dir=Path("runs/custom"))
config.max_http_requests = 40
config.request_budget_path = config.work_dir / "http_attempts.json"
config.deadline_monotonic = time.monotonic() + 1800
config.request_timeout_s = 120
config.model_profiles = False
run_config = RunConfig(tool_steps=5, calls_per_question=32)
```

This snippet makes no requests. Multiple role clients should share `request_budget_path` for a global counter. `deadline_monotonic` is an absolute request deadline covering queue waits, streaming, and retries; `request_timeout_s` also bounds transport waits. SDK callers must separately bound the entire task; CLI `--timeout` already bounds the complete demo.

Advanced overrides include `model_strong`/`model_middle`/`model_fast`, their corresponding endpoint/key fields, and `role_tiers`. `model_profiles=True` explicitly opts into compatibility parameters, rather than enabling vendor reasoning settings by default. Historical dataset routing loads legacy configuration explicitly at the dataset boundary; it is not the generic default.

`Config` controls model connections; `RunConfig` controls fixed execution limits. Neither is an optimization asset. A changed model, route, or source requires a new experiment directory. See [quickstart](quickstart.md) for CLI setup.

## Explicit advanced overrides

This constructs configuration only: no clients or requests. Endpoint/model values are placeholders. `OPTIONAL_FAST_API_KEY` is read explicitly by this example, not automatically by the generic CLI. After replacement, extraction/tools use the fast tier; remaining roles use the main connection.

```python
import os
from pathlib import Path
from darwinagent import Config

config = Config.from_env(work_dir=Path("runs/advanced"))
config.model_fast = "your-fast-model"
config.fast_base_url = "https://fast-provider.example/v1"
config.fast_api_key = os.environ.get("OPTIONAL_FAST_API_KEY", "")
config.role_tiers = {**config.role_tiers, "extraction": "fast", "tools": "fast"}
config.model_profiles = True
config.reasoning_effort = "low"
config.thinking_disabled_roles = {"extraction", "tools"}
```

Provider profiles and reasoning parameters require support from the selected model/endpoint; this example opts in explicitly. Defaults add no such provider parameters. Use a new experiment directory after changing routing.
