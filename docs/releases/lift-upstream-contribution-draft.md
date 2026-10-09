# Draft only: native DarwinAgent runtime adapter for LIFT

Unsent contribution draft. No upstream issue, PR or message has been created.

## Proposed contribution

Add an optional `darwinagent` runtime adapter against LIFT commit
`9d282e415c5e3b0f65b82c8758a013b269583d93`. It implements the native
`ContainerAgentRuntimeAdapter` / `ChatAgent` lifecycle: warmup, evolution hook, commit,
fresh-container load and holdout. A registration wrapper currently leaves upstream source
untouched. DarwinAgent's existing proposal/Wiki/selection services retain candidate assets;
LIFT's native execution and content-evaluation protocol remain the benchmark authority.

Worker/judge tools execute in isolated containers without model credentials. Shared host
transport accounts for all roles and quarantines unknown submissions. Macro leaves retain
individual receipts/spans. Judge workspaces are read-only, separate from evolved state;
accepted assets reside in the container's commit layer and are checked on fresh loading.
Pinned task materials reject extra files and escaping symlinks before container startup.

## Reproduction and validation supplied with a future PR

Provide the [reproduction guide](../en/agent-runtime-reproduction.md), pinned dependency
list, optional adapter sources and a non-secret acceptance summary. The current Docker/native
fixture uses 81 recorded responses and validates hooks, delta restoration, trace readback,
full per-cell/role accounting and native report generation. It is mechanism evidence only.
No live quality claim, independent q6 result or trajectory-judge result is included.

The all-pair report supplements upstream filtered summaries so incomplete/failed/unstarted
cells and cumulative costs stay visible. CSV/HTML trajectory placeholders are labeled
`not_run` until independently evaluated. Contribution scope should keep this optional,
avoid core dependency additions, and retain upstream third-party notices/licenses.

## Actual development diagnosis supplied as limited evidence

Three preserved source identities yielded two framework protocol failures and one budget pause:
`aadb5b4` lacked bounded feedback for a settled non-JSON reply (JSON mode was already enabled);
`669b94d` did not explain/validate nonempty object-array artifacts; `68a0084` repairs both with
scoped parse/prevalidation feedback and no repeat of settled writes. Recorded before/after
counterexamples prove recovery mechanics, not improved model quality.

The third fresh run completed q1 first delivery, received native intermediate false/0.33
assessment for delivery format, then exhausted the cumulative eight-request worker cap during
correction. Across all attempts and availability, 26 actual HTTP completed, zero unknown,
244539 tokens (workers 101410, judge 143064, probe 65). A ninth local worker reservation was
rejected before HTTP; its native token total is null with known subtotal 47613. q2,
evolution/candidate and q5 A/B/C never started; K0 remains unchanged, q6 sealed, trajectory
judge not_run. No final score, actual comparison or live CSV/HTML is supplied.

Trace readback covers completed worker turn 0 (5 generations/4 leaves) and judge
(8 generations/4 leaves). Three correction requests/three leaves persist in journals but were
not flushed to trace-state. The retry-overwritten native task_time_s is not task wall time;
measured LIVE003 request elapsed totals are worker 60.9758 s and judge 391.4317 s.
The stable budget pause is retained; remaining global budget is not permission to bypass the
exhausted task cap. All raw failures remain local; only safe facts and hashes accompany this draft.

## Questions for upstream review when submission is authorized

- Preferred optional runtime registration/distribution convention and pinned compatibility policy.
- How full failure/cost rows and trace-version compatibility should appear in native reports.
- Whether a recorded-response lifecycle fixture belongs in upstream integration checks.

Submission, licensing review of any copied upstream material and live evidence inclusion
remain future work requiring explicit authorization. This draft changes no upstream files.
