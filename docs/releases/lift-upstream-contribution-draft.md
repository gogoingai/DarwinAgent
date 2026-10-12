# UNSENT draft: optional DarwinAgent integration for pinned LIFT

**Unsent contribution draft. No upstream issue, pull request or message has been sent.**
Prepared 0.3.0 is unpublished. q5 and independent q6 measurements are complete and the current
wheel passed outside-checkout installed checks. Sdist checks, scoped documentation review and
all 14 requirements passed within approved local framework, real MiniMax q5/q6 verification and
unpublished package preparation.
This is not a submission or release.

## Full local extension: implementation complete, real evaluation pending

The measurements, package checks, and three-round protocol below describe the historical
single-scene pilot; they are not full-benchmark acceptance for the new extension.

The full local extension at source commit `9066d4b` covers all 14 pinned EALE scenes, with
56 training/validation cells and 84 A/B/C holdout cells (140 required executions). It uses
30 native interaction rounds uniformly, adds bounded web search/read tools, and invokes
the native trajectory-judge prompts/parser through the same paid MiniMax request ledger.
Every scene selection is frozen before any holdout execution. q1–q3 supply evolution
inputs; q4 exposes only permitted validation aggregates. Sales q5/q6 are already observed
historical tests and are identified separately.

A complete recorded native-execution smoke settled all 140 cells with 434 recorded calls,
zero actual HTTP, and terminal recovery with no new calls or changed files. Recorded
scores cannot satisfy real acceptance. Independent review found and fixed a report case
that could mark acceptance true while the shared gate was paused; the focused complete-matrix
regression passes. These checks establish mechanism behavior, not real task quality.

Real full-run `minimax-full-v030-001` has started locally using the existing cumulative
MiniMax ledger. Its original policy and 307 historical receipts remain unchanged. Full
coverage, C quality, evolutionary gains, and release acceptance are **not yet verified**.
See the full protocol guides: [English](../en/lift-full-local.md) /
[简体中文](../zh-CN/lift-full-local.md). Any future submission must attach complete
real results, failure/regression records, and all role costs before making performance claims.

## Proposed contribution

Provide an optional integration against LIFT commit
`9d282e415c5e3b0f65b82c8758a013b269583d93`, outside the DarwinAgent core wheel.
A registration wrapper leaves pinned upstream source untouched. The existing container adapter
implements `ContainerAgentRuntimeAdapter` / `ChatAgent` lifecycle hooks, including warmup,
evolution, commit, fresh-container loading and holdout. The current evaluated route is a separate
**LOCAL task-execution adaptation**, using upstream `execute_task`, `run_task`, native content-judge
prompt, criteria, parser and scoring without Docker, Langfuse or full container trace.

DarwinAgent's existing CampaignController, EpisodeExperiment, ProposalSession and Wiki services
provide candidate construction, admission and selection. Full S/F/C/P asset declarations are
exposed under fixed tool/ExecutionProfile contracts; unsupported authority changes are rejected.
Actual MiniMax candidate `5556a044...` changed worker P only. Local tool workspaces are independent;
judge tools are read-only and private criteria/credentials/ledgers/assets stay outside the workspace.
Primitive leaves remain individually checked and counted, including macros and optional batches.

The explicit host-side MiniMax policy uses `MiniMax-M3.1-Flash-Preview`,
`https://api.minimax.cn/v1`, temperature 0, adaptive/max and generation 16384 all roles, sent as
`max_completion_tokens` with no legacy reasoning buffer or provider fallback. Null model-call
caps are opt-in and retain durable accounting, unknown-effect quarantine and fixed three native
interactions/64 leaves per episode. Generic API defaults and legacy example HTTP 100/worker 8 remain.

## Reproduction and validation that may accompany a future PR

Supply the [reproduction guide](../en/agent-runtime-reproduction.md),
[optional adapter documentation](../../datasets/lift/README.md), dependency/version pins and a
non-secret [acceptance record](../acceptance/2026-10-09-agent-runtime.md).
Frozen source `97c6cbb` passed 692 discovered framework tests (684 passed,8 resource skips),
LoCoMo 18, TravelPlanner 11 and Ruff/format 330. Current-source wheel verification passed in the
existing Python 3.11 environment outside the checkout with four episodes and zero modelHTTP;
SHA 256 `9d3466ec0ef1d7d151d61cbaa57553b26443f426832896520b51cde543c9c32b`.
The audited sdist excludes private artifacts/configured credentials and rebuilds a wheel
byte-identical to the installed wheel. The check uses documentation commit `5e069e0`; final
scoped documentation review of `c4b7726` and all 14 requirements passed. Final archive/manifest
regeneration and content verification are the coordinator's separate distribution step.

Healthy/failure local unbounded-v3 fixtures passed with 57/166 recorded calls and 0 model/infrastructure
requests. Replay added 0 calls and 0 receipt/artifact writes; the failure fixture explicitly fell
back to K 0. Scores/tokens are synthetic mechanism evidence. The retained historical container
fixture used 81 recorded responses and checked lifecycle hooks, delta restore, trace readback and
postprocessing. It does not establish current MiniMax container/trace acceptance or model quality.

All-pair reports retain final/intermediate values, failed/unstarted arms, fallback and all role
costs. Native trajectory placeholders stay `not_run`. An all-pair report supplements rather
than replaces upstream filtered summaries, so task failure and cost regressions remain visible.

## Measured local development result and limits

The q2 selection metric rose 0.5833→0.75 with success 0→0 and accepted a worker-P-only candidate.
q5 A/B/C all received healthy final native content evaluations, with content
0.6666666667/0.6/0.8 and success=false for every arm. All used three native interactions/two
corrections. Worker model calls 14/20/30 and primitive leaves 27/43/49 show that higher C content
also incurred greater execution cost; B's content score is lower than A's. These results do not
support task-success, efficiency-improvement or generality claims.

Development used 218 actual model HTTP attempts, all completed: worker 124, judge 85, proposal 9.
MiniMax availability 2 is reported separately. Final local report scope is explicitly
`local_q5_A_B_C_execution_verification_only`, with `quality_improvement_claim:false`.
Independent q6 completed with A/B/C content 0.79/0.86/0.93 and native success=false for every arm.
All evaluations are healthy/final; worker calls 15/18/20 and leaves 24/28/59 show greater C costs.
This supports content-score increase on one independent task only, not task-success, efficiency
or generality improvement. Confirmation 89 adds to development 218 for 307 completed benchmark
requests; two availability probes yield 309 total MiniMax attempts. Package checks, scoped documentation review and all 14 requirements passed within
approved local verification/preparation scope; neither
execution verification nor these scores establish full benchmark/release acceptance.

Historical GLM 335/500 remains 334 completed and 1 unknown with run 007 paused, unchanged. The unknown
cause cannot be reconstructed from old receipts. New allowlisted stream diagnostics and durable
terminal-result handling do not resolve that historical request. GLM scores/costs are not spliced
into MiniMax quality conclusions. Earlier 26/100 protocol and budget failures remain archived.

## Items for upstream review after submission authorization

- Preferred optional registration/distribution convention and pinned compatibility policy.
- Whether LOCAL native-protocol verification and container lifecycle verification should have
  explicitly distinct report scopes.
- Representation of final null scores, failed/unstarted cells, macro/leaf costs and all role overhead.
- Placement of recorded-response lifecycle/recovery fixtures and native trace-version compatibility.

Local scoped documentation review and all 14 requirements passed. Submission and copied-material
licensing review remain future work; the draft is still UNSENT.
Frozen q6 measurements are documented without private task/criteria/reason text. No upstream
files or external communication were changed by this draft.
