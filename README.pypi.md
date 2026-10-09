# DarwinAgent

**Evolution for the Agent Era**

An open Python framework for experience-driven recursive self-improvement of agents. Run a task, propose changes to its assets, evaluate candidates independently, and retain effective versions and experience. Version 0.2.0 is experimental; improvement must be measured.

[中文使用指南](https://github.com/gogoingai/DarwinAgent/blob/main/README.zh-CN.md) · [English guide](https://github.com/gogoingai/DarwinAgent/blob/main/README.md)

## Install

Python 3.11 or newer is required. One package provides both the command-line tool and the Python API:

```bash
python -m pip install --upgrade darwinagent
darwinagent --version
```

## Configure a model

Live execution requires your own OpenAI-compatible Chat Completions endpoint. Create `.env` in the directory where you run commands:

```dotenv
DARWINAGENT_BASE_URL=https://your-provider.example/v1
DARWINAGENT_MODEL=your-model-name
DARWINAGENT_API_KEY=your-api-key
```

Replace all three placeholders with your API base URL, supported model name, and key. Do not include `/chat/completions` in the base URL. All roles share this model by default. The CLI loads the current directory's `.env`; existing environment variables take precedence.

## Run the live demo

Check the connection with one real model request, then run two rounds:

```bash
darwinagent doctor --check-model --output runs/doctor
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

The demo asks who maintained a device and when, then tries to improve its prompt using independent answer scoring. Results go to `runs/demo-live/`. Start with `demo-summary.json` for decisions and reasons; `published/current.json` points to retained assets.

The cap counts HTTP attempts including retries; the timeout bounds the whole run. Ties are rejected and scores need not improve. Add `--resume` to continue an existing run, or use a fresh output directory for another experiment.

## Use the Python API

Install the same package, then use the [complete live Python example](https://github.com/gogoingai/DarwinAgent/blob/main/docs/en/quickstart.md#4-call-it-from-python). It explicitly loads `.env`, creates `Config`, and passes it to `run_demo(..., mode="live", config=config)`.

For your own records and questions, follow the [custom task tutorial](https://github.com/gogoingai/DarwinAgent/blob/main/docs/en/custom-tasks.md), including a complete `Pipeline` script and an independent evaluator.

## Try without model credentials

```bash
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
```

Replay uses scripted model responses with no network requests or credentials. Expect completion, a first accepted candidate, and a second rejected on a tie. It demonstrates mechanics, not model learning or benchmark gains.

## Durable continuation and Wiki queries

Keep saved model responses and tool results across safe restarts and human interventions. Working and adopted candidates are separate. A proposer can query training originals and request fresh Wiki regrouping in the same dialogue, with explicit coverage and evidence references. Unknown requests require explicit recovery or retry. See [workspace operations](https://github.com/gogoingai/DarwinAgent/blob/main/docs/workspace-continuation.md) and [live-loop acceptance](https://github.com/gogoingai/DarwinAgent/blob/main/docs/plans/intervention-wiki-loop-acceptance-20261008.md).

## Documentation

- [Quickstart](https://github.com/gogoingai/DarwinAgent/blob/main/docs/en/quickstart.md)
- [Model configuration and troubleshooting](https://github.com/gogoingai/DarwinAgent/blob/main/docs/en/configuration.md)
- [中文快速开始](https://github.com/gogoingai/DarwinAgent/blob/main/docs/zh-CN/quickstart.md)
- [Source and issues](https://github.com/gogoingai/DarwinAgent)

Distributed under the MIT license.
