# DarwinAgent 0.3.0 implementation plan

Approved specification: the user's 2026-10-09 plan, implemented in the isolated
`feature/agent-runtime` worktree based on v0.2.0. Do not merge, push or publish.

## Global constraints

- Preserve v3 bundle bytes/fingerprints and old graph-QA behavior. New bundles are v4.
- Support external Agent loops, real files/artifacts, S/F/C/P evolution, and immutable receipts.
- Source facts and execution events are immutable; schema-derived graphs are rebuildable.
- S/F changes with P fixed must have observable behavior. Unsupported schema declarations fail.
- F uses a bounded interpreter and async registered capabilities, never arbitrary host Python.
- C runs before/after actions and at final delivery. Host checks and tool permissions stay fixed.
- Record every leaf tool call; macro calls cannot hide costs. Unknown writes never auto-replay.
- Public APIs: AgentAdapter.run(task, context), AgentContext, EpisodeResult, ExecutionBackend.
- Experiments consume task/artifact episodes without fake question/corpus wrappers.
- Reuse ProposalSession, Wiki, asset fingerprints, durable request budgets and recovery.
- Live model: glm-5.3-flash, https://open.bigmodel.cn/api/coding/paas/v4, temperature 0.
- All actual HTTP attempts across all roles/processes share a non-resetting 100-request ledger.
- No secrets in source, logs, images or delta. No automatic retry of unknown requests.
- Native LIFT commit 9d282e415c5e3b0f65b82c8758a013b269583d93; EALE revision
  419789ed1662e7b9ad16aa6620a4b31449612948. Sales scenario q1 train, q2 validation,
  q5 development diagnosis, q6 sealed independent confirmation.
- A plain ReAct; B same Agent plus K0; C same Agent plus selected K1. Identical privileges,
  parameters and per-task budgets. At most one evolution round/valid candidate.
- First run max 3 total interaction turns (at most 2 corrections) and 8 execution-model requests per task/session across turns.
- Native scoring, all pairs/failures, leaf counts and full evolution overhead must be reported.
- Quality cannot decline. Quality ties rank correction rounds, leaf tools, then worker tokens.
- Train/validation/test evidence is isolated. Source changes require fresh experiment identity.
- Retain failures. Publish quality claims only with independent frozen-version evidence.

## Task 1: generic execution core and v4 kernel

Implement `src/darwinagent/agent/` contracts, v4 bundles/profile, schema-guided task and
trajectory graphs, bounded async F, lifecycle C, AgentContext, registered tools and a
minimal ReAct adapter. Add meaningful unit/integration tests with two independent Agent
implementations. Export APIs from the package while preserving legacy APIs. Integrate v4
load dispatch into KernelBundle without changing v3 verification or serialized bytes.

Core contracts: AgentTask has id, instruction, attachments, public parameters.
EpisodeResult carries status, final response, artifacts, events/trace and measured usage.
AgentContext exposes async model/call and view/check; adapters retain their own loops.
ExecutionProfile freezes prompt roles, capabilities and check stages outside mutable assets.
For baseline experiments kernel=None exposes the same registered primitive tools.
Provide backend run/admit/identity functions usable by Task 2, with explicit durable run IDs.
Reuse existing journal/transport infrastructure; test interrupt/restart receipt reuse and
unknown write quarantine, source-bound graphs, S/F-only changes and rejected final delivery.

## Task 2: backend-driven experiments and evolution

Integrate execution backends with ExperimentRunner/CampaignController, keeping the old
default backend and evaluator contracts. Implement episode evaluator support, independent
quality/efficiency selection, Wiki/ProposalSession driven v4 candidate proposals/admission,
and durable accepted-version retention. No prompt-only workaround or independent optimizer
that bypasses existing proposal/Wiki infrastructure. Validation/test raw evidence cannot
enter training Wiki. Add isolation, budget, quality-tie and no-candidate tests.

## Task 3: native LIFT adapter, datasets, bounded campaign and reporting

Add optional benchmark code outside the core wheel under datasets/lift. Provide native
ContainerAgentRuntimeAdapter/ChatAgent implementations and registration patch/script for
the pinned upstream. Worker and judge use the common host-side transport budget. Containers
contain tools and exported kernel/Wiki state, not keys. Persist evolution assets beneath
/opt/darwin-state (not a bind mount); each task material directory/workspace is fresh.
Implement isolated file/text/table/transform/write capabilities shared by A/B/C, native
warmup/delta/load/holdout semantics, q1/q2/q5 split, one candidate, all-pair cost report and
reproduction config. q6 stays unopened until a frozen independent confirmation.
Test native hooks, delta reload, workspace boundaries, repeated-hook idempotency and counters
offline before any API calls. Real API calls are only made by the coordinator once all
environment prerequisites pass. Docker/trace environment must be ready; no homemade judge
fallback presented as native LIFT. Budget exhaustion records partial state and pauses.

## Task 4: verification, diagnosis and release preparation

Run focused tests, complete framework/LoCoMo/TravelPlanner regressions, Ruff and installed
wheel verification. Run the bounded live diagnostic only after native environment tests
pass. Preserve artifacts/failures and fix framework defects with before/after evidence;
changed source refreezes all comparison groups. Add bilingual SDK/examples/migration docs,
0.3.0 release preparation, a reproducible report and upstream contribution draft. No release,
push or external messages. Never claim unrun/partial results are completed or improved.
