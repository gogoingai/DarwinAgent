# DarwinAgent: Evolution for the Agent Era

![DarwinAgent hero](../assets/hero-en.png)

A correct answer shows that an agent succeeded on one run. The more interesting question is whether it can retain useful capabilities, learn from the run's experience, and adapt when it encounters a similar task again.

DarwinAgent is an open framework for experience-driven recursive self-improvement. Its name comes from Charles Darwin and the theory of evolution. Its mission is **Evolution for the Agent Era**. Evolution has a concrete engineering meaning here: propose variations, select through independent evaluation, retain effective versions, and let experience inform the next proposal.

The current 0.1.0 version is experimental. It provides a runnable, inspectable, resumable loop. Any quality gain still needs to be established in a specific task with an independent evaluation protocol.

## From one run to an evolution loop

![Evolution loop](../assets/evolution-loop-en.png)

DarwinAgent starts with a baseline, then proposes task-asset changes using training evidence and an experience Wiki. A candidate must satisfy contracts, permissions, and behavioral trials. It then produces answers through the shared runtime and receives scores from an independent evaluator. A candidate that does not strictly improve the primary metric, or violates the frozen rules, is rejected.

Accepted assets are atomically published as a new version. Rejected candidates also supply experience: their rejection reasons and observations enter the persistent Wiki and become available to subsequent proposals. Experience includes failures and ineffective changes as well as successes.

This loop does not train model weights or grant agents arbitrary access to rewrite framework execution. Evolution inspires the project's name and design; it is not a claim to implement a genetic algorithm or guarantee progress every round.

## What may change

![Asset boundary](../assets/asset-boundary-en.png)

The current proposal boundary contains four task-asset types: S schemas, F restricted query functions, C graph/answer checks, and P fixed-role prompts. Execution flow, permissions, fixed source checks, independent evaluation, and adoption rules remain outside the boundary.

A candidate must pass the existing evaluation instead of changing evaluation to pass. Runs retain asset fingerprints, sources, answers, scores, proposals, and decisions so that a reviewer can inspect what actually changed.

## A common runtime, two task interfaces

![Architecture](../assets/architecture-en.png)

DarwinAgent 0.1 uses a graph-based runtime: extract attributed facts, assemble a typed graph, then query, answer, and review. Custom tasks implement an input adapter and an independent evaluator, register their assets, and execute through the common `Pipeline`. Evaluation references remain inside the evaluator rather than entering generation inputs.

The core package includes a tiny maintenance task that can run offline or connect to one explicitly configured OpenAI-compatible endpoint. Arbitrary external agent plugins are not yet available.

## Run the mechanism before claiming improvement

From the current source checkout:

```bash
uv sync --frozen
uv run darwinagent doctor --output runs/doctor
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
```

Replay uses scripted model responses, a seeded baseline, and P-only proposals through the actual runtime, evaluation, adoption, and Wiki stages. One synthetic question is scored for technician/date coverage, producing 0.5 → 1.0 → 1.0: one acceptance followed by rejection of a tie. It makes no API requests and has no held-out set. These scores demonstrate mechanisms, not model learning or benchmark improvement.

Live mode uses the same task and independent evaluator without a scripted fallback. A model may answer the baseline correctly, so retaining it and rejecting two tied candidates can be the right result. Request limits count retries and the timeout covers the whole demo.

The experimental 0.1.0 source is available on the repository's `main` branch and has not been published to PyPI. See the [README](../../README.md) and [quickstart](../en/quickstart.md) for installation, live configuration, and SDK integration. The [dated acceptance record](../acceptance/2026-10-07.md) separates local regression, replay, and real-model smoke evidence.

DarwinAgent aims to make agent adaptation a bounded experiment with evidence that accumulates. Next directions include more independent tasks, controlled held-out studies, and a defined external agent integration boundary. Visit the [repository](https://github.com/gogoingai/DarwinAgent) to inspect the implementation or contribute reproducible issues and task examples.

---

The S/F kernel is inspired by the [OaK paper](https://arxiv.org/abs/2608.22974); C/P and the experience Wiki are this project's engineering extensions. MIT applies to the project; third-party material retains its own terms. [中文版本](introduction.zh-CN.md).
