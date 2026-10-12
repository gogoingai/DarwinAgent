# Full local LIFT evaluation with MiniMax

[简体中文](../zh-CN/lift-full-local.md)

This optional repository integration covers all 14 scenes in the pinned EALE dataset. It reuses native LIFT execution, content judging, and trajectory-judge prompts and parsing locally, without Docker or Langfuse. It is outside the core wheel and requires repository sources, an existing Python 3.12 environment, and pinned upstream dependencies.

## Protocol

Pin LIFT to `9d282e415c5e3b0f65b82c8758a013b269583d93` and EALE to `419789ed1662e7b9ad16aa6620a4b31449612948`. The dataset has 56 training tasks and 28 test tasks.

Within each scene, q1/q2/q3 supply training trajectories and evolution evidence. q4 validates candidates; only permitted aggregate metrics reach the proposer. Finish and freeze every scene selection before executing q5/q6 across A (minimal ReAct), B (the same Agent with K0), and C (selected K1). Rejected candidates explicitly fall back to K0, with a separate C execution.

Required coverage is 56 training/validation cells plus 84 A/B/C test cells: 140 total. Candidate admission and extra validation/evolution requests are additional costs. All arms share the model and primitive permissions, with at most 30 native interaction rounds per task. Keep this protocol separate from the historical three-round sales diagnostic.

Primitive tools list files, read text and tables, calculate over tables, write files, search the web, and read web pages. Expand composite functions into separately recorded primitive calls. Web tools do not execute JavaScript or arbitrary host code. Tasks requiring unsupported capabilities may fail and remain in the complete matrix.

## Execution and recovery

The current CLI extends an existing MiniMax shared budget directory. It requires the original policy, receipts, and cumulative HTTP counter to reconcile, with no unresolved request. Adding the trajectory-judge role creates a separate audited enrollment and leaves the original policy and receipts unchanged. This recovery route does not initialize an empty budget directory.

Copy the [example configuration](../../datasets/lift/minimax.full.example.json), setting private data paths, a fresh run directory, and the existing budget directory. The dataset requires `full-download-manifest.json` with pinned file hashes; verify downloaded objects against the upstream Git/LFS objects. Inject `DARWINAGENT_API_KEY` privately at runtime. Keep credentials out of configuration, version control, and model context.

The following command makes real requests and requires authorization for the model, budget, and network tools:

```sh
DARWINAGENT_MODEL=MiniMax-M3.1-Flash-Preview \
DARWINAGENT_BASE_URL=https://api.minimax.cn/v1 \
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.full \
  --upstream /path/to/pinned/LIFT \
  --config /private/full-config.json \
  --run-id NEW_FULL_RUN
```

The fixed request policy uses temperature 0, adaptive thinking, reasoning_effort=max, and max_completion_tokens=16384. Request limits in the example are null and require explicit unbounded-call authorization. This does not mean free execution. Execution, graph construction, Wiki, proposals, content judging, and trajectory judging use the existing cumulative HTTP ledger.

Resume with identical arguments, configuration, and run identity. Settled terminal receipts are reused without redispatch. Known task failures are retained while other tasks continue; unknown outcomes pause the shared gate. Source, configuration, tool, or input changes require a fresh experiment identity and new A/B/C executions. Never overwrite original failures or splice scores from different versions.

## Reading results

`full-matrix-report.json/.csv` include every required cell, unstarted work, failures, native content success/scores, separate trajectory scores, corrections, expanded primitive calls, and role costs. Current-run costs are separate from shared historical costs.

`coverage_complete` requires all cells to have terminal outcomes. `evaluation_healthy` requires complete valid evaluation evidence. `c_quality_satisfied` requires native success=true and content_score=1 for every C test cell. `acceptance_complete` additionally requires full scene coverage, verified current-run paid model receipts, and no shared pause. Recorded clients and synthetic scores cannot satisfy real acceptance.

Historical sales q5/q6 were already observed; identify them separately instead of presenting them as fresh independent generalization evidence. Completed execution alone does not prove evolutionary gains. Public comparisons must include A/B/C differences, all regressions, and additional costs.
