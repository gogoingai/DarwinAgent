"""Run the installed demo with a model configured in the local .env file."""

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
