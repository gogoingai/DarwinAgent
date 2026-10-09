# Connect an Agent you already own

[简体中文](../zh-CN/agent-sdk.md) · [Migration](migration.md) · [Reproduction](agent-runtime-reproduction.md)

The prepared, unpublished 0.3.0 runtime accepts tasks and artifacts directly. Your Agent owns
its loop; DarwinAgent supplies admitted capabilities, S/F/C/P assets, lifecycle checks and
durable receipts. The old `Pipeline` and graph-QA CLI remain available.

## Run the complete offline example

From this prepared source checkout, use Python 3.11 or newer:

```sh
uv sync --frozen --group test
uv run --frozen python -m darwinagent.demo.agent_runtime --output runs/agent-offline
# Equivalent source entry point:
uv run --frozen python examples/agent_runtime.py --output runs/agent-offline
```

Or build and install the local wheel with `uv build` and
`python -m pip install dist/darwinagent-0.3.0-py3-none-any.whl`, then invoke the same module
from any directory. No endpoint, key or network model is required. The packaged
[example source](../../src/darwinagent/demo/agent_runtime.py) is self-contained.
It runs both `OwnLoopAgent` and `ReActAgent`, creates four real file artifacts, changes F
while keeping P fixed, catches a missing optional file, and verifies receipt reuse.
`summary.json` contains each episode's events and usage. Recorded responses demonstrate
mechanisms; their absent token usage and zero model HTTP must not be sold as efficiency gains.

## Public contracts

```python
from darwinagent import AgentTask, ExecutionBackend, ExecutionProfile, ToolFailure
from darwinagent.agent import file_tools

class MyAgent:
    def identity(self):
        return {"policy": "report-v1"}

    async def run(self, task, context):
        try:
            source = await context.call("read_file", {"path": "source.txt"})
            text = source["content"]
        except ToolFailure as failure:
            if failure.error_type != "FileNotFoundError":
                raise
            text = task.instruction
        artifact = await context.call("write_file", {"path": "report.txt", "content": text})
        return context.result("Saved report", artifacts=(artifact,))

# Execute within your application's async entry point:
async def run():
    task = AgentTask("report", "A report from public task inputs")
    profile = ExecutionProfile("files-v1", ("read_file", "write_file"))
    backend = ExecutionBackend(
        MyAgent(), file_tools("runs/my-agent/workspace"), profile,
        run_root="runs/my-agent/receipts",
    )
    return await backend.run(task, kernel=None, run_id="report-1")
```

`AgentTask(id, instruction, attachments=(), parameters={})` accepts immutable JSON data.
Keep references and private grading criteria outside these public inputs.
`EpisodeResult` holds status, response, artifacts, events and measured usage. Status is
`completed`, `rejected`, `failed`, `unknown` or `budget_exhausted`.

`AgentContext` exposes `await model(messages, role=...)`, `await call(name, params)`,
`view()`, `check(stage, candidate)`, `prompt(role)` and `result(...)`. An external adapter
may use any loop over these operations. ReAct is a supplied example adapter, not the API
boundary. Ordinary classes must explicitly implement synchronous `identity() -> dict`;
include behavior-changing configuration and external versions. Dataclasses use their fields
unless they override identity. Transient counters should not become configuration.

For live use supply your explicitly configured client to `ExecutionBackend(client=...)`.
Its async `chat(**kwargs)` returns `content`, `usage` and `model`; the production `LLMClient`
uses `Config`. Register each profile role (e.g. `cfg.role_tiers['worker'] = 'strong'`).
The host chooses endpoint, model, credentials and HTTP limits. No model is selected by assets.

## What a v4 kernel changes

| Asset | Execution effect | Fixed boundary |
| --- | --- | --- |
| S | Projects typed nodes/relations from task, attachments and actual events; preserves source references | Does not edit source records; unsupported declarations fail |
| F | Executes bounded async macros with `call` and `view`; each call awaits a registered primitive | No host Python/import/subprocess access; no nested macros |
| C | Checks before/after primitive actions and final delivery | Cannot remove host checks, permissions or stages |
| P | Supplies prompts through registered role slots | Cannot choose model/provider or expand tool permissions |

Construct `AgentAsset`, `AgentKernelAssets(assets, profile)` and export to a new directory;
load with `AgentKernelBundle(path)` or `KernelBundle(path)` format dispatch. Each non-S
asset declares its S dependency, every profile prompt role has one P, and every F supplies
contract-valid `trial_inputs`. `backend.admit(kernel)` validates static interfaces; meaningful
behavior and quality still require execution and independent evaluation. The example gives
a complete S/F/C/P bundle and demonstrates an actual F-only output change.

`ExecutionProfile` freezes capabilities, host checks, roles and limits outside evolving
assets. Each C must return literal `True`. A rejected post-action check cannot undo a write
that already happened; retain that artifact and receipt while withholding successful delivery.

A bound host check must expose synchronous `identity()` or `declaration()` on its owner,
returning a stable JSON object that includes all policy configuration. Callable objects use
the same declaration contract. Functions with captured values, defaults or global data also
need an explicit declaration; ordinary stateless functions remain supported directly.
Declarations are hashed, so private configuration does not appear in profile or run receipts.
A changed declaration invalidates completed replay and campaign identity; use fresh run IDs.


## Recovery and isolation

Every durable run requires an explicit safe `run_id` and `run_root`. The same identity
reuses settled responses and results. Changed task, source, tool/profile, model policy,
adapter configuration or kernel requires a fresh run ID; never rewrite old identities.

Known read failures become stable `ToolFailure` receipts and may be handled by the Agent.
Replaying them preserves the original error even if the outside file later appears.
Submitted writes or model requests without authoritative receipts remain `unknown`; they
are quarantined and never automatically replayed. Preserve the evidence and explicitly
recover an authoritative receipt or make an authorized retry decision. Do not remove pause
files or call a lost response a measured zero-token operation.

Campaigns with write tools require `backend.for_episode(episode_root, task) -> backend`.
The packaged `WorkspaceBackend` provides a concrete factory: bind tools under that episode's
workspace and receipts under its allocated directory, preserving both on resume. Stage only
that task's pinned materials. Preserve class/source/profile/adapter/transport identity and
all tool declarations; only declared location fields vary. Shared write workspaces fail
admission. Physical isolation of a remote location remains the host's responsibility.

## Experiments and accounting

Pass the backend to `CampaignController` or `ExperimentRunner` with `AgentExperimentSpec`,
an `AgentDataset` and an `EpisodeEvaluator`. These public contracts accept real tasks and
`EpisodeEvaluation`; no dummy corpus/question wrapper is needed. Freeze the evaluator's
`declaration()`, source, model, data and budget. Train/validation/test are disjoint; admission
may reuse training. Only public training evidence enters the proposal Wiki. Candidates use
the existing ProposalSession/Wiki/admission/retention path.

Quality cannot decline. Equal quality compares correction turns, flattened leaf calls and
worker tokens, in that order; unknown costs do not become free improvements. Every macro
leaf has its own receipt and cost. Episode `model_calls`/`leaf_calls` count reservations;
`pending_calls`, `unknown_calls`, and `failed_leaf_calls` explain unresolved or failed work.
Actual HTTP attempts (including retries) come from the shared transport ledger, across
worker, judge, proposal and Wiki roles. Report both layers, evolution overhead, failures,
unstarted cells and unavailable usage. A local response fixture establishes neither live
quality nor generality; see the dated [acceptance record](../acceptance/2026-10-09-agent-runtime.md).
