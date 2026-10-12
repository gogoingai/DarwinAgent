# Reproduce generic Agent acceptance

[简体中文](../zh-CN/agent-runtime-reproduction.md) · [SDK](agent-sdk.md)

Prepared, unpublished 0.3.0 at frozen source `97c6cbb`.
The current MiniMax run is a LOCAL adaptation of pinned LIFT task execution and content judging.
q5 and independent q6 A/B/C measurements are complete; the current wheel passed installed checks.
Sdist package checks passed; final documentation review and requirement audit remain pending.
The dated [record](../acceptance/2026-10-09-agent-runtime.md) separates measurements from history.

## Core checks and actual installed package

The following commands reproduce the checks; actual results are recorded separately:

```sh
uv sync --frozen --group test
uv run --frozen --group test python -m unittest discover -s tests -v
uv run --frozen --group test python -m unittest discover -s datasets/locomo/tests -v
uv run --frozen --group test python -m unittest discover -s datasets/travelplanner/tests -v
uv run --frozen --group test ruff check .
uv run --frozen --group test ruff format --check .
uv run --frozen python examples/agent_runtime.py --output runs/NEW-agent-offline
uv build --wheel
```

Coordinator evidence on `97c6cbb`: 692 framework tests discovered, 684 passed, eight explicit
resource skips; LoCoMo 18 and TravelPlanner 11 passed; Ruff and format 330 passed. Keep original
logs and diagnostics; skips are unrun resources, not passing assertions. No model HTTP was
used for these regressions. The current wheel passed in the existing Python 3.11 verification
environment outside the checkout, with four episodes and zero model HTTP. To reproduce, install
the newly built wheel in a clean Python 3.11 environment, then run
`/path/to/clean/python /absolute/checkout/scripts/check_installed.py` from outside the checkout.
It checks the site-packages import, independent Agent/ReAct loops, actual file artifacts,
settled replay and legacy doctor/demo/resume/Wiki behavior. Historical wheel hashes or older
73/26/69 checks do not establish that the current wheel is installed and working.

## Public API and opt-in null model-call limits

`AgentAdapter.run(task, context)` owns its loop and returns `EpisodeResult`. `AgentTask` carries
instruction, public parameters and attachments; `AgentContext` exposes model/call/view/check;
`ExecutionBackend` binds execution, host policy and durable receipts. `EpisodeResult.artifacts`
is an array of objects. Refer to the [SDK guide](agent-sdk.md) for complete construction.

Generic defaults remain `ExecutionProfile.max_model_calls=8`, `ReActAgent.max_turns=16` and
core generation 4096. Explicit None removes only the corresponding model-call/decision cap:

```python
from darwinagent.agent import ExecutionProfile, ReActAgent

profile = ExecutionProfile(
    "explicit-model-unbounded", ("read_text", "write_text"),
    max_model_calls=None, max_leaf_calls=64,
)
agent = ReActAgent(max_turns=None, max_tokens=16384)
```

These objects do not select a provider, register tools or disable host checks. S/F/C/P assets
remain subject to the fixed capability and lifecycle contracts. Every model call still
increments persisted counters; unknown requests/writes never replay automatically. Existing
v3 bundles, graph-QA paths and finite/default identities remain supported. An old capped K 0
cannot be silently reused for a new null-cap execution.

## Current local MiniMax reproduction policy

Use the existing Python 3.12 environment and pinned LIFT checkout
`9d282e415c5e3b0f65b82c8758a013b269583d93`. Development data is the fixed EALE
`419789ed1662e7b9ad16aa6620a4b31449612948` q1/q2/q5 manifest; q6 remains unopened during development.
Do not provision Docker, Langfuse, new services or dependencies for this local route.

Copy [minimax.local.example.json](../../datasets/lift/minimax.local.example.json) and assign
independent private run/data/budget paths. Use a fresh MiniMax budget identity; preserve the
historical GLM budget and its unknown pause. Within this MiniMax identity all authorized roles,
processes and new comparison roots share the same ledger. Supply the credential only through
host `DARWINAGENT_API_KEY`. The model/endpoint environment must exactly match the configuration.
Only the authorized coordinator executes this real-model command:

```sh
DARWINAGENT_MODEL=MiniMax-M3.1-Flash-Preview \
DARWINAGENT_BASE_URL=https://api.minimax.cn/v1 \
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.local \
  --upstream /path/to/pinned/LIFT --config /private/minimax-local.json \
  --run-id UNIQUE_MINIMAX_DEVELOPMENT_RUN
```

The explicit policy `minimax-adaptive-v1` uses generic routing (`model_profiles=False`),
`thinking.type=adaptive`, `reasoning_effort=max`, temperature 0 and generation 16384 for worker,
judge, proposal and Wiki. The JSON key is `max_tokens`; the actual MiniMax request parameter
is `max_completion_tokens`, with no extra legacy reasoning buffer and no model/endpoint fallback.
Generic and previously registered providers retain their existing outgoing token conventions.

`unbounded_model_calls:true`, `worker_limit:null` and `http_cap:null` are explicit opt-ins.
Worker session/turn, judge, proposal and Wiki model-call limits are actual None/null rather
than a large sentinel. The three native interactions (two corrections), primitive permissions,
64 leaf calls per episode, one candidate and one evolution round remain fixed. SDK retries
remain 0 and a submitted unknown request still closes the shared gate. Removing a call cap does
not remove durable accounting, deadlines, unknown-write isolation or identity verification.
The legacy [pilot.example.json](../../datasets/lift/pilot.example.json) stays HTTP 100/worker 8.

Local tools are `list_files`, `read_text`, `read_table`, `transform_tables`, `write_text`, scoped
to independent task workspaces. Judge tools are read-only. No arbitrary host code or child
process is exposed. Private criteria, credentials, selected assets and ledgers are outside the
workspace. Dotenv discovery and native pre-chat reporting are disabled. Native content-judge
prompts, criteria, parser and scoring remain authoritative through this local adaptation;
the result is wrapped as a string in the outer `final_response` envelope.

## Recorded local mechanism checks

Use fresh output directories. These commands consume recorded responses, not real model calls:

```sh
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-q1-q2-q5-data \
  --unbounded --output /private/NEW_minimax_healthy
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-q1-q2-q5-data \
  --unbounded --failures --output /private/NEW_minimax_failures
```

Frozen healthy-v3/failure-v3 recorded checks passed with 57/166 calls, zero model HTTP and zero
infrastructure calls. Healthy selected a fixture candidate; failure continued known failures
and explicitly selected K 0 fallback. Both replays added zero model calls and zero receipt/artifact
writes. Scores/tokens are synthetic mechanism evidence, not MiniMax quality or savings.
Earlier v1/v2 failures remain preserved; their acceptance is not inferred from v3.

## Selection, development and independent confirmation

q1 public task evidence may be reused across warmup/admission/training for an identical task,
kernel and identity. q2 private validation is excluded from training Wiki. The existing
CampaignController/EpisodeExperiment/ProposalSession/Wiki flow exposes complete S/F/C/P authority;
unsupported declarations and fixed-permission changes fail admission. Only one candidate is
submitted. Proposal/Wiki calls and original queries are counted even when feedback warns of
repeated evidence or repairs a copied continuation. Raw sources and old stored cursors remain
readable; paging is checked at 8000 and 512 wire-character bounds without changing source bytes.

A is plain ReAct, B adds K 0, C loads the selected K 1 or explicit K 0 fallback. After selection
freezes, q5 A/B/C run independently; C=K 0 still does not reuse B. The observed real candidate
changes worker P only. q2 content 0.5833→0.75 with success 0→0 justified selection, not independent
quality improvement. q5 A/B/C all have healthy final evaluations but success=false, with content 0.6666666667/0.6/0.8.
Worker calls 14/20/30 and leaves 27/43/49 show greater execution cost for C. All arms used three
native interactions/two corrections. Negative results must be retained.

Only after development scheduling finishes may the coordinator prepare a separate q6-only
confirmation manifest and fresh root. Set `mode:confirmation`, `selection_run_root` to the
completed development root, and the q6-specific `data_root`. Use the same frozen source,
model/request policy, generation, effective caps, ledger, K 0 and selected assets. Confirmation
performs independent q6 A/B/C without training, proposal, Wiki or evolution. The frozen MiniMax
confirmation completed with healthy final content scores 0.79/0.86/0.93 and native success=false
for A/B/C. All arms used three interactions/two corrections; worker calls were 15/18/20 and
leaves 24/28/59. C used more worker calls, tokens and leaves. Its native task duration includes
an execution suspension caused by the coordinator misreading the GLM-only pause scope; the
user instructed MiniMax to resume. Task/request elapsed timings are not comparative latency.
This establishes higher
content scores on this one independent task, not task success, efficiency or generality.
Safe metrics and all costs appear in the acceptance record; private q6 text/criteria are not exported.

## Results, failures and historical boundaries

`all-pair-report.json/.csv/.md` includes all planned arms, terminal and intermediate scores,
failures, unstarted rows, fallback and measured worker/judge/proposal/Wiki calls/tokens/durations.
Leaf calls count individually, including macros and optional 1–16-action batches; all batch
contracts validate before effects and known failures stop remaining actions. ReAct keeps one
latest view while retaining chronological observations and immutable receipts.

A healthy native evaluation can report success=false. A completed comparison can show no
improvement or regression. Missing final quality/tokens are null, never invented zero; interim
scores cannot replace final results. Development benchmark accounting is 218 completed HTTP, worker 124/judge 85/proposal 9,
2533327 tokens and 1403.6033 summed request seconds; two MiniMax availability calls are separate.
The report acceptance scope is q5 execution verification only, with quality_improvement_claim=false;
independent q6 execution verification and package checks are complete, while final documentation
review and requirement audit remain pending. Confirmation added 89 completed requests (worker 53, judge 36), 1042902 tokens and
648.5198 summed request seconds. Cumulative benchmark 307 plus availability 2 equals 309 MiniMax
attempts; benchmark tokens total 3576229. Historical GLM 335 remains separate.

Known task failures can settle and independent scheduling can continue. Unknown model receipts,
uncertain writes and applicable shared gates pause new work without automatic replay. New
MiniMax diagnostics store allowlisted stage/type/status/stream counters and hashed response IDs;
completed terminal streams survive cleanup failures/cancellation without losing settled receipts.
These repairs do not reconstruct the cause of the old GLM 007 request.

Historical GLM remains 335/500 (334 completed, 1 unknown), paused and unchanged. Earlier 26/100 and
container/trace/81-recorded-response records are archived in the acceptance record. They are
separate source identities and do not describe current MiniMax acceptance. The current local
route has no Docker/Langfuse/full container trace or independent trajectory-judge result; it is
not full-container or full-benchmark reproduction. No merge, push, tag, release or upstream
message occurred. Real measurements and wheel/sdist checks passed: the audited sdist excludes
private artifacts/configured credentials and rebuilds a byte-identical installed wheel. The check
uses documentation commit `5e069e0`; the coordinator regenerates the final archive after the
remaining documentation review/audit. The installed wheel hash is
`9d3466ec0ef1d7d151d61cbaa57553b26443f426832896520b51cde543c9c32b`.
