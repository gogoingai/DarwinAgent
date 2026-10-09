# Migration

[English](../en/migration.md) · [简体中文](../zh-CN/migration.md) · [README](../../README.md)

## Prepared 0.3.0: opt into generic Agent execution

0.3.0 is a locally prepared, unpublished version, explicitly approved for the new runtime
architecture. Ordinary compatible releases still increment only the patch number.
Install from this checkout or its local wheel; PyPI's verified public version remains 0.2.0.

- Existing `Pipeline`, default `ExperimentRunner`/`CampaignController`, CLI and v3 bundles
  retain the graph-QA route. No blanket v3-to-v4 rewrite is performed. V3 manifests, asset
  bytes, fingerprints, histories and evaluation locks stay immutable.
- New Agent tasks use `AgentTask`, `ExecutionBackend` and a v4 `AgentKernelBundle` with a
  fixed `ExecutionProfile`. Explicit `backend=` opts experiments into this route; it is not
  a drop-in conversion of a QA checkpoint. See the [SDK and example](agent-sdk.md).
- Ordinary adapter classes declare `identity()`. Catch structured `ToolFailure` for known
  read failures. Write or model submissions with lost receipts remain `unknown`.
- Supply explicit run IDs and a per-episode workspace factory for write tools. Changed
  code/model/data/tool/evaluator identities require a fresh run; preserve prior directories.
  Resume original frozen runs with their original source/dependencies instead of rewriting
  their identities to match this version.
- Core installation supports Python 3.11+. Optional native LIFT runs from the source tree in
  the pinned Python 3.12 environment and adds no unconditional core imports/dependencies.

## Historical package migration: 0.1.0

DarwinAgent 0.1.0 has a new package identity; earlier historical framework versions are not directly comparable by version number. Both distribution and import name are `darwinagent`; no old-namespace compatibility shim is provided.

1. Install with `python -m pip install darwinagent`, or check out the source for development, and use `from darwinagent import ...`; changing imports alone does not migrate a frozen run.
2. Generic model connections use `DARWINAGENT_API_KEY`, `DARWINAGENT_BASE_URL`, and `DARWINAGENT_MODEL`. Historical dataset configuration loads explicitly at the dataset boundary.
3. Create a new output directory and freeze source/assets/model/evaluator identity again. Old runs require their original source and dependencies; do not recompute old locks to bypass identity checks.
4. Tasks implement `DatasetAdapter` and `Evaluator`, register S/F/C/P, and use the common `Pipeline`. That version did not provide external Agent plugin entry points; the prepared 0.3.0 SDK above adds this route.
5. The core wheel excludes root datasets, historical outputs, tests, and third-party environments. Demo resources are packaged.

The [history index](../history/README.md) retains earlier docs and research outputs. Historical scripts remain byte-preserved and may require their original checkout. Start with the current [quickstart](quickstart.md); old docs do not describe the current API.

## Historical migration: engineering governance on 2026-10-07

The package version was 0.1.0 during that migration; moving or formatting implementations changed source identity.
That migration required a new output directory. Matching new-source checkpoints can resume;
old-source checkpoints must fail identity validation without rewriting the original declaration.

That migration used `evaluation_lock.governance-20261007.json`, covering its original four files
plus `pipeline/dates.py` and the shared `operators/calendar.py`. The historical
`evaluation_lock.json` remains byte-for-byte intact and intentionally rejects current source.
The evaluator's explicit `lock_path` remains available for runs with their corresponding source.
The new lock records source identity, not a new benchmark result or a changed scoring policy.
See [provenance and acceptance](engineering-governance.md).


## Explicit HF input on 2026-10-09

Current LoCoMo runs require a commanded HF repository/revision or an explicit local source.
The resolved HF commit and raw-file hashes are recorded independently of cache location.
The current evaluator interface uses `evaluation_lock.governance-20261009.json`; earlier locks
remain unchanged. Original QA metrics are the default, and audited references are explicit inputs.
