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

## Questions for upstream review when submission is authorized

- Preferred optional runtime registration/distribution convention and pinned compatibility policy.
- How full failure/cost rows and trace-version compatibility should appear in native reports.
- Whether a recorded-response lifecycle fixture belongs in upstream integration checks.

Submission, licensing review of any copied upstream material and live evidence inclusion
remain future work requiring explicit authorization. This draft changes no upstream files.
