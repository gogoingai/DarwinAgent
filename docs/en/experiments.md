# Experiments and evidence

[English](../en/experiments.md) · [简体中文](../zh-CN/experiments.md) · [README](../../README.md)

![Evolution loop](../assets/evolution-loop-en.svg)

`ExperimentRunner` freezes identity and executes B0 → proposal → admission → candidate generation/evaluation → adoption → Wiki. The independent evaluator supplies metrics. `AdoptionPolicy(primary, non_decreasing)` requires strict primary improvement, no decline in named guard metrics, and checks completeness/faults. External transport faults are disclosed separately from deterministic generation faults; evaluation faults still block adoption. Inspect the actual decision reasons.

The demo uses `run_demo(..., rounds=2)`, seeded B0, and P-only changes, without validation or test sets. It does not replace a held-out study. `ExperimentRunner` can apply per-round validation gates; this is distinct from a three-set campaign.

`CampaignController` uses disjoint train, validation, and test case IDs from `ExperimentSpec`. After training it locks candidates, validates/selects versions, then runs a one-time test. `SelectionPolicy` uses independent metric names. This protocol is implemented, but the demo and real smoke acceptance did not execute a complete three-set study.

```python
from darwinagent import ExperimentSpec, AdoptionPolicy, SelectionPolicy

spec = ExperimentSpec(
    train=("train-case",), validation=("validation-case",), test=("test-case",),
    adoption=AdoptionPolicy("correct", ()),
    selection=SelectionPolicy("correct", "correct"), rounds=2,
)
```

This creates a protocol declaration; it makes no requests and supplies no dataset implementation. Multi-case aggregation uses the frozen sum rule: evaluators should supply additive metric counts. The demo's single-case accuracy is not a ready-made multi-case average.

## What to retain

Preserve asset versions, source/model identity, case sources and answers, scores, proposal inputs, admission/rejection reasons, Wiki, and request counts. Resume only matching frozen identities; never rewrite old locks to admit changed source. Dataset studies require additional data/evaluator dependencies listed through the [history index](../history/README.md); these are not in the core wheel.

The [2026-10-07 report](../acceptance/2026-10-07.md) separates local regression, offline replay, and the real smoke. Request count and elapsed time are not token usage or monetary cost.
