# Reproduce generic Agent acceptance

[简体中文](../zh-CN/agent-runtime-reproduction.md) · [SDK](agent-sdk.md)

This describes prepared, unpublished 0.3.0. All ordinary checks below use recorded/local
responses. Native LIFT and the separate trajectory judge are different acceptance layers.
The dated [record](../acceptance/2026-10-09-agent-runtime.md) states actual results and limits.

## Core and installed package

```sh
uv sync --frozen --group test
uv run --frozen --group test python -m unittest discover -s tests -v
uv run --frozen --group test python -m unittest discover -s datasets/locomo/tests -v
uv run --frozen --group test python -m unittest discover -s datasets/travelplanner/tests -v
uv run --frozen --group test ruff check .
uv run --frozen --group test ruff format --check .
uv run --frozen python examples/agent_runtime.py --output runs/NEW-agent-offline
uv build
```

Create a clean Python 3.11 environment, install the local wheel, then run
`/path/to/clean/python /absolute/checkout/scripts/check_installed.py` from outside the
checkout. This checks the actual site-packages import, both generic loops, file artifacts,
settled replay, and legacy doctor/demo/resume with immutable Wiki bytes. Python 3.12 is the
verified optional native environment; the core does not import its benchmark dependencies.
Historical external/formal-resource skips are not passed assertions. Preserve full logs;
do not relax the existing startup-time threshold to obtain a passing label.

## Native recorded-response acceptance

Use the pinned LIFT checkout `9d282e415c5e3b0f65b82c8758a013b269583d93`, the separate
Python 3.12 environment with `datasets/lift/requirements.lock`, and the exact EALE
`419789ed1662e7b9ad16aa6620a4b31449612948` q1/q2/q5 material manifest.
Do not open or stage q6. Provision local trace infrastructure first; keep its credentials
outside version control. [Adapter instructions](../../datasets/lift/README.md) document
capabilities, limits and registration. Run with an explicit Docker context:

```sh
DOCKER_CONTEXT=colima-darwin-lift docker build -t darwinagent-lift-tools:task3 datasets/lift
DOCKER_CONTEXT=colima-darwin-lift DO_TRAJECTORY_JUDGE=false \
/path/to/python3.12 -m datasets.lift.tests.offline_native \
  --upstream /path/to/pinned/LIFT --data /private/pinned-data \
  --output /private/NEW-native-run --trace --trace-env /private/infra/.env
/path/to/python3.12 -m datasets.lift.tests.trace_probe \
  --upstream /path/to/pinned/LIFT --root /private/NEW-native-run --trace-env /private/infra/.env
```

Do not change the global Docker context. The fixture executes real containers, tools,
upstream content-evaluation protocol, evolution hooks, fresh delta loading and trace
readback/postprocessing, using 81 recorded responses and zero model HTTP requests.
Scores and tokens are synthetic; local wall time is not network latency. Native report
trajectory placeholders always mean `trajectory_judge=not_run`.

## Bounded live development diagnosis: partial and paused

Current outcome (2026-10-10): LIVE001 on `aadb5b4` failed on a non-JSON settled reply;
LIVE002 on `669b94d` failed on string-array artifacts. Bounded protocol feedback and explicit
object-array validation repaired those framework boundaries, with recorded before/after tests.
Fresh LIVE003 on `68a0084` completed q1 first delivery, whose native intermediate assessment
was false/0.33, then exhausted the eight-request worker cap during correction. Final task score
is unavailable. All 26 actual HTTP completed (17 worker, 8 judge, 1 probe), totaling 244539 tokens.
No unknown submission exists; one ninth local reservation was denied before HTTP. Its native
worker total remains null, known subtotal 47613. q2, evolution/candidate and q5 A/B/C are
unstarted; K0 is unchanged, q6 sealed, trajectory judge not_run. No further calls are authorized.
Keep the stable pause and ledger; remaining global budget does not permit a task-cap bypass.

Only completed worker turn 0 (5 requests) and judge (8 requests) traces were read back;
three completed correction requests remain in local journals, not flushed trace-state.
No live native CSV/HTML was produced. The retry-overwritten task_time_s field is not wall time;
LIVE003 measured request elapsed totals are worker 60.9758 s and judge 391.4317 s.
See the acceptance record for all failures, unstarted rows and costs. Full 642-pass evidence
belongs to earlier `aadb5b4`; current source has 73/26/69 covering checks. This docs-only update
preserves the tested wheel/source and rebuilds sdist without repeating unrelated suites.


Only the coordinator runs real model calls after review and a fresh source freeze. The
approved configuration is `glm-5.3-flash`, temperature 0, Coding endpoint
`https://open.bigmodel.cn/api/coding/paas/v4`, identically for all roles.
Use at most 3 interaction turns (2 corrections), 8 worker requests per task/session and one
candidate/evolution round. All actual HTTP attempts across worker, judge, proposal, Wiki,
availability probes, processes and fresh run directories share one stable 100-request ledger.
Keep `budget_root` separate from `run_root`; source changes never reset the budget.

A is plain ReAct, B adds K0, C loads the selected K1 (or retained K0 if rejected). q1 is
training/admission; q2 validates; q5 is development diagnosis. Freeze code, model, backend,
kernels, evaluator, data and per-task permissions/budgets before comparison. Any repair after
q5 preserves old failures, creates a new identity and reruns all A/B/C with remaining budget.
If that cap prevents completion, report partial. q6 remains sealed and independent quality
confirmation is unrun. No live quality or generality claim follows from the offline fixture.

The authoritative `all-pair-report.json` includes every planned cell and its arm/phase,
status, success/content, correction turns, flattened leaf tools, native tool counts, role
model calls/tokens, measured durations, failures and unknown values. Retain incomplete turns
and unstarted arms. Report proposal/Wiki overhead and total HTTP separately; missing token
usage is unknown. A filtered upstream summary cannot replace these rows or hide declines.
Unknown submitted requests pause all new transport until explicitly resolved; never silently
retry or clear pause state.

Keep private local `.superpowers/`, dataset materials, credentials and raw judge evidence on
disk. Export only non-secret aggregate facts and hashes to tracked reports. For each defect,
record reproduction command, original failure, cause, scoped repair and covering retest;
original artifacts are immutable evidence.

### Settled task failures and the shared request gate

The native LIFT plugin retains a terminal task receipt after a known worker/judge
failure or the unchanged eight-worker-call session limit. Independent q2 and q5
A/B/C executions continue with the same three native rounds. A failed warmup skips
proposal work, runs q2 as an independent diagnostic, explicitly rejects promotion,
and materializes K0 for C. An unhealthy campaign also falls back to K0; this is not
evidence of a successful evolution. Partial files, turn receipts, trace increments,
original durations, and failures remain available; replay sends no model requests.

Submitted unknown requests, uncertain writes, trace failures, and the shared
100-HTTP-attempt limit still block all independent work. A denied reservation is
not a submitted unknown request. `all-pair-report.json` distinguishes failed,
blocked, cancelled, and unstarted cells. Failed final quality is null; a previous
judge score is labelled intermediate. Pinned native reports cannot represent
unjudged null scores. The plugin therefore retains failed pairs in its all-pairs
report and completes C independently even when B fails; it never substitutes zero
or claims native CSV/HTML acceptance for an omitted pair.

The explicit `SharedTransport.recover_local_cap_pause` API audits only the legacy
bare `{"status":"budget_exhausted"}` pause. It requires the exact historical worker
session, expected current HTTP count below 100, complete reconciled request
receipts, eight worker calls, a matching local-cap journal denial, and no unknown
write/request or trace failure. It preserves the original pause bytes and counters
and creates an immutable recovery acknowledgement. It does not recover newer
explicit global gates. Only the coordinator may apply this audit to the historical
live ledger. Source changes require a fresh run identity for all comparison arms;
historical runs remain separate and the shared HTTP ledger continues unchanged.
