# DarwinAgent

**Evolution for the Agent Era** · **Agent 时代的进化**

An open framework for experience-driven recursive self-improvement of AI agents.
DarwinAgent takes its name from Charles Darwin and the theory of evolution:
propose changes to task assets, select through independent evaluation, retain
effective versions, and use a persistent experience Wiki to inform subsequent
proposals.

**Version 0.1.0 is experimental.** Improvement is an outcome to measure, and a
candidate can be rejected. The framework does not train model weights or rewrite
its own optimizer.

## Install and try the loop

Python 3.11 or newer is required. In your Python environment:

```bash
python -m pip install darwinagent
darwinagent --version
darwinagent doctor --output runs/doctor
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay --resume
```

Replay requires no API key or network. It executes the actual controller, task
runtime, evaluation, admission, Wiki, and asset publication with scripted model
responses. In one tiny synthetic maintenance task, the first candidate is accepted
and the second is rejected on a tie; HTTP attempts are zero. This demonstrates
the mechanism and does not establish model learning or benchmark gains.

For real model execution, set `DARWINAGENT_API_KEY`, `DARWINAGENT_BASE_URL`, and
`DARWINAGENT_MODEL` to your OpenAI-compatible Chat Completions endpoint, then run:

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

Live mode calls the configured model. The request cap counts dispatched HTTP
attempts, including retries, and the timeout covers the whole run.

## Bring your own task

Register S/F/C/P assets: schemas, query functions, task checks, and role prompts.
Connect a dataset adapter and an independent evaluator to the shared `Pipeline`
and `ExperimentRunner`. The framework's executor, evaluator, permissions, and
adoption policy remain outside the proposal boundary.

## Documentation

- [English README](https://github.com/gogoingai/DarwinAgent/blob/main/README.md)
- [中文说明](https://github.com/gogoingai/DarwinAgent/blob/main/README.zh-CN.md)
- [Quickstart](https://github.com/gogoingai/DarwinAgent/blob/main/docs/en/quickstart.md)
- [中文快速开始](https://github.com/gogoingai/DarwinAgent/blob/main/docs/zh-CN/quickstart.md)
- [Acceptance evidence](https://github.com/gogoingai/DarwinAgent/blob/main/docs/acceptance/2026-10-07.md)
- [Source and issues](https://github.com/gogoingai/DarwinAgent)

DarwinAgent is distributed under the MIT license.
