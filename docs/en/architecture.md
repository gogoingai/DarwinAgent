# Architecture

[English](../en/architecture.md) · [简体中文](../zh-CN/architecture.md) · [README](../../README.md)

![Architecture](../assets/architecture-en.svg)

DarwinAgent 0.1 uses a common graph-based task runtime. The dataset boundary implements only `DatasetAdapter.generation_input(case_id)` and async `Evaluator.evaluate(result)`, returning `CaseInput` and `EvaluationResult` respectively. Evaluator references do not enter generation inputs.

`Pipeline.run(case, spec, run_config)` freezes identity and records artifacts. `ExtractionAgent` extracts attributed facts and deterministically assembles a typed graph. `AnswerAgent` queries registered functions, answers, and reviews evidence under fixed protocols. Both agents share `KernelRuntime`. Framework code owns fixed validation and publication; task C checks return opinions.

`TaskSpec` declares assets; `KernelBundle` carries relocatable, fingerprinted versions. S defines schema; F queries via read-only operators and a restricted AST interpreter; C checks graph or answer snapshots; P fills registered role slots. The [asset boundary](#asset-boundary) defines what proposals cannot change.

`ExperimentRunner` is the public composition facade; its original hooks remain usable by
recorded runners and task injections. `CampaignController` adds the three-set protocol.

| Experiment module | Responsibility |
| --- | --- |
| `runner` | Validation, dependency assembly and thin extension hooks |
| `lifecycle` | Declaration/source identity, seed or cold-start B0 and baseline preparation |
| `stages` | Execution, independent scoring/checkpoints and smoke/preflight |
| `optimization` | Wiki/legacy proposals, recovery and admission attempts |
| `rounds` | Round budget, validation, decision, Wiki and publication coordination |
| `graph_trials` | Frozen/dynamic/rebuilt graph supply and cache |
| `constants` | Shared feedback budget and default admission attempts |
| `feedback`, `recovery`, `trials`, `statistics`, `snapshots` | Training evidence, recovery, trials, statistics and snapshots |

Helpers receive explicit dependencies and callbacks without importing the runner facade.
Private iteration state updates after durable decision/publication and adds no serialized fields.
Recovery precedes STOP/round limits; resumed rounds retain their original budgets.

`WikiMaintainer` owns event persistence, deduplication, attribution and refresh. `wiki_evidence`,
`wiki_lessons` and `wiki_context` process bounded evidence, verified lessons and proposal context.
Admission samples/checks/functions/composition return ordered batches; the entrypoint/reporting
collector preserves checkpoints and fail-closed rejection.

Adapters transform domain inputs; evaluators hold references; run entries assemble components;
exports convert outputs. `operators/calendar.py` shares identical primitives only; domain
resolvers and answer equivalence stay separate. Reusable offline fixtures live in `tests/support`,
independent of TestCase/scenario files. See the [governance report](engineering-governance.md).

## Asset boundary

![Asset boundary](../assets/asset-boundary-en.svg)

Proposals can modify S/F/C/P. Framework execution, read-only permissions, fixed source/status checks, `RunConfig`, evaluator, and adoption rules sit outside that boundary. C cannot replace fixed checks; P cannot add unregistered slots; F cannot gain arbitrary Python access.

New LoCoMo g1 runs construct graphs with an LLM under the current S. Wiki and the proposer share evidence from graph construction, tools, checks and answers; see [dynamic graphs and asset change signals](dynamic-graph.md).

Source entry: [`src/darwinagent`](../../src/darwinagent/__init__.py). The core wheel includes only `darwinagent` and demo resources. Root `datasets`, `tasks`, `tests`, independent baselines, and third-party environments are not core distribution contents. Arbitrary external agent plugins are not implemented.
