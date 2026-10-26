# Full local LIFT evaluation with MiniMax

[简体中文](../zh-CN/lift-full-local.md)

This optional repository integration covers all 14 scenes in the pinned EALE dataset. It reuses native LIFT execution, content judging, and trajectory-judge prompts and parsing locally, without Docker or Langfuse. It is outside the core wheel and requires repository sources, an existing Python 3.12 environment, and pinned upstream dependencies.

## Most recent terminal audit (2026-10-14 09:42, Beijing time)

Fresh run 010 completed PPT q1 training healthily: native success, content score 1.0, 11 interaction rounds and trajectory score 0.84. Elapsed time was 1710.44 seconds (about 28.5 minutes). It used 57 worker, 36 content-judge and one trajectory-judge request: 94 actual model requests and 2660103 service-reported input/output tokens. All 50 expanded primitive calls remain recorded, including seven known failures (three provider challenges and four HTTP failures), with no unknown or pending operations.

The complete trajectory was rebuilt from original journals and matched to the actual judge input. For all 11 rounds, the judge-read artifact hash matches that round's final delivery. The audit uses this run's actual filename, `ppt_beginner_guide.html`; the failed audit script assuming the older filename remains retained. This verifies receipt/version correspondence, not independent correctness of judge decisions. Evidence: experiment-directory `infra/full-training-diagnostics/231-*` and `232-*`.

This is complete evidence for one training task, with no A/B/C effectiveness or efficiency conclusion. Three-arm evaluation, q6 and acceptance of the 140-cell full matrix remain incomplete. The next task continues under the same frozen identity and cumulative ledger.

## Most recent verified snapshot (2026-10-14 09:10, Beijing time)

`minimax-full-v030-010` has created the 14-scene, 140-cell main matrix and received its first real MiniMax response. Its reviewed recovery-accounting repair resolves explicitly authorized replacement replies for current logical totals while retaining and validating original submissions, grants and replacement receipts. The candidate passed 111 offline LIFT regressions and 41 execution/recovery checks; its launcher passed 13 admission checks and actual offline runner construction. These checks do not demonstrate improved real-task quality.

Run 009 stopped at a fully receipted boundary with the cumulative budget at 3608. Run 010 uses that same ledger; this snapshot has 3610 attempts, including one completed and one in-flight request from the new run. Both startup failures occurred before HTTP dispatch and their original logs remain. The candidate's missing historical-source binding was registered through the existing additive lineage mechanism without changing original authorization, pause records or counter hashes; no unknown request was retried or abandoned. Evidence: experiment-directory `infra/218-*`, `219-*`, `220-*`, and `224-*` through `228-*`; 219/220 are under `infra/full-training-diagnostics/`.

After three new consecutive confirmed 429 responses, the local supervisor first confirms physical requests and tool operations are settled, then suspends the same executor for five hours before resuming it, recording wait start/end. It makes no model calls. Automatic dotenv discovery, Langfuse reporting and native automatic trajectory judging are disabled; this integration's explicit trajectory judging remains budgeted. No old scores are imported. A/B/C and q6 remain incomplete with `full_real_acceptance=false`. This is a dated verified snapshot, not evidence of subsequent progress.

## Historical snapshot (2026-10-13 08:16, Beijing time)

Fresh run 009 has two completed and one failed main cell. PPT q4 is scheduled while the service remains in rate-limit cooldown. q3 completed 21 content-judge rounds with last interim score 0.963, then three consecutive HTTP 429 responses exhausted the current worker step retries. It failed without a successful final content result or trajectory evaluation; 0.963 is not a successful final score. Elapsed time was 2720.00 seconds, with 221 actual requests and 8161189 known input/output tokens. Three failed requests returned no usage, which is not reported as zero cost.

All 114 expanded primitive operations have receipts; 23 tool failures remain recorded, with no unknown or pending worker operations. Ten action JSON rejections and their 289449-token cost were separately verified against original replies. Scheduling continued to q4 after the failed cell, without resetting the budget. Evidence: experiment-directory `infra/full-training-diagnostics/203-*` through `206-*`. Three-arm evaluation, q6 and full acceptance remain incomplete; the long-trajectory repair has no terminal validation from this fresh q3 task.

## Historical snapshot (2026-10-13 07:29, Beijing time)

Fresh run 009 completed PPT q1/q2 and continued to q3: 2/140 main cells complete. q2 achieved native success and content score 1.0 in 13 rounds, with trajectory score 0.58 and elapsed time 1044.45 seconds. It used 58 worker, 48 content-judge and one trajectory-judge request: 107 actual requests and 2864538 input/output tokens in total. There were 42 expanded primitive calls, no tool failures and no unknown or pending worker operations. The complete trajectory and artifact versions for all 13 rounds were verified.

The round-eight decrease from 0.59 to 0.52 remains recorded. Its artifact retained all prior characters and added 1856 characters; the judge read the correct version. The cause is unresolved. These training records expose correction cost and do not establish three-arm effectiveness. Evidence: experiment-directory `infra/full-training-diagnostics/195-*` through `199-*`. Three-arm evaluation, q6 and full acceptance remain incomplete; the ledger was not reset.

## Historical snapshot (2026-10-13 07:15, Beijing time)

Fresh run 009 completed PPT q1 and continued to q2: 1/140 main cells complete. q1 achieved native success and content score 1.0 in nine rounds, with trajectory score 0.84 and elapsed time 1094.80 seconds. It used 68 worker, 32 content-judge and one trajectory-judge request: 101 actual model requests and 2915362 input/output tokens in total. All 67 expanded primitive calls and 13 tool failures remain recorded; the worker has no unknown or pending operations. The complete trajectory was reconstructed against original receipts, and all nine judges read artifacts matching their corresponding rounds. Evidence: experiment-directory `infra/full-training-diagnostics/189-*` and `190-*`.

This is one healthy training result, with no three-arm effectiveness or efficiency claim. The executor was verified live and the budget ledger was not reset. Scene selection, three-arm holdout and q6 remain incomplete; full acceptance remains open.

## Historical snapshot (2026-10-13 06:53, Beijing time)

The long q3 trajectory passed a real MiniMax retest: HTTP 200, native trajectory score 0.66, one additional request, 588987 input tokens and 2589 output tokens, about 33.32 seconds. Original input, failure receipt and new diagnostic result remain separate. This is neither an old experiment score nor evidence of improved Agent quality. Run 008 ended at a fully receipted boundary; original files and budget were unchanged. It is marked superseded and incomplete.

The real replay exposed and repaired a rate-control cache incorrectly treated as immutable. The current audit chain must remain valid while old event prefixes and request receipts remain immutable. The completed result then replayed with no new request, leaving the cumulative ledger at 3065. The 009 launcher also repaired a double configuration read that could separate hash validation from parsed configuration. The 16 recovery gate and 10 full-admission checks passed independent review.

Fresh `minimax-full-v030-009` has started with its new frozen identity, all 14 scenes, 140 main cells and the original ledger. Its live process and initial real HTTP 200 replies were verified; it is rerunning PPT q1. Three-arm and q6 evaluation are incomplete, so neither acceptance nor effectiveness claims are justified. Failures remain visible and all scene selections must freeze before holdout. Evidence: experiment-directory `infra/full-training-diagnostics/182-*`, `185-*`, and `186-*`.

## Historical snapshot (2026-10-13 06:03, Beijing time)

The `minimax-full-v030-008` process was verified live again. Of 140 required main cells, three completed and one failed; sales weekly analytics q1 is running. A/B/C holdout has not started. The preceding scene's q4 completed healthily: native success, content score 1.0, 14 rounds, trajectory score 0.66, 1136.46 seconds, 73 worker model calls and 59 expanded primitive calls. The worker has no unknown or pending tool operations. This validation record is not an A/B/C result.

q3 passed native content judging with score 1.0, but complete trajectory judging received a confirmed HTTP 400 context limit error. Its cell remains failed, original input and receipt are retained, and scheduling continues. The shared ledger contains 2871 actual requests at this snapshot, including one in progress. This run has no new 429 or unknown requests; accounting was not reset.

An offline long-trajectory representation repair retains complete candidate prompts and the reconstruction script. It addresses literal marker ambiguity and rejects rehashed noncanonical encodings. Eight tests pass. The q3 candidate has about 1.41 million characters, and reconstruction produces the exact original native judge input. It is not integrated into the frozen live runtime; MiniMax score equivalence and real usability remain unverified. Adoption requires a fresh experiment identity and new A/B/C runs. Full evaluation, q6 and acceptance remain incomplete.

Additional snapshot (06:06): sales weekly analytics q1 completed healthily in four rounds, with native content score 1.0 and trajectory score 0.82. The main matrix now has four completed cells and one failed cell; sales q2 is running. The shared ledger contains 2896 actual requests, including one in progress. The v003 offline trajectory prototype adds a visible content index to every content container; nine tests pass and the complete candidate contains about 1.48 million characters. Native input reconstruction matches exactly, but paid-entry admission, real scoring and production integration remain pending. Original v001/v002 artifacts and failures are retained.

Additional snapshot (06:16): sales q2/q3 completed healthily with native content score 1.0; q4 is running. The main matrix has six completed cells and one failed cell. The long-trajectory candidate runtime passed independent review and was frozen with 311 source files. All 104 native integration checks and 31 rate/receipt checks pass; the single-read admission gate passes six checks. Real retained q3 reconstructs the reviewed prototype messages exactly. Active run 008 retains its original frozen source. The candidate has not been adopted live; real scoring, new A/B/C executions and full acceptance remain pending.

Additional snapshot (06:23): sales q4 completed healthily with content score 1.0, five rounds and trajectory score 0.98. The main matrix has seven completed cells and one failed cell; the active real request is in Wiki maintenance. The diagnostic004 input component passed five checks and independent review, reconstructing complete original worker evidence before matching the prepared messages. The launcher only has partial duplicate-dispatch rejection controls and stays closed by default; paid dispatch and run-transition acceptance remain incomplete.

## Latest run status (2026-10-13 05:04 China Standard Time)

`minimax-full-v030-008` is running with verified process liveness and all 140 required cells across 14 scenes registered. Both q1 and q2 training tasks completed with healthy evidence. q3 is underway; required-cell progress is 2/140.

q1 achieved native success, content score 1.0, ten interaction rounds and trajectory score 0.84: 753.98 seconds, 83 actual requests, 33 expanded primitive calls and 2365687 prompt/completion tokens. q2 achieved native success, content score 1.0, fifteen rounds and trajectory score 0.72: 1567.61 seconds, 142 actual requests, 67 expanded primitive calls and 5846973 prompt/completion tokens. Both complete trajectories were independently rebuilt read-only and checked against original receipts. All six known web-tool failures in q2 remain retained; unconfirmed operations are zero. These training results establish neither A/B/C gains nor scoring-representation equivalence.

The fresh availability diagnostic used one actual request, establishing a shared launch baseline of 2293. The cumulative ledger is 2520 at this snapshot, with no new 429 or unknown outcomes in this run. The frozen rate-control and recovery runtime remains unchanged; prior failures, scores and costs are retained. A separate receipt-verification fix passed independent review and 122 offline tests but is not used by this run; adoption requires a fresh frozen identity and new executions of every arm. A/B/C, q6 and overall acceptance are incomplete.

Offline installation and compatibility checks passed: 685 framework checks passed with eight external-resource skips, plus all 18 LoCoMo and 11 TravelPlanner checks. Private installed-wheel examples and recovery passed outside the checkout. Dependencies were reused rather than installed fresh, and no public release occurred. Historical snapshots below retain their original reporting dates and cannot supply new-version scores.

## Protocol

Pin LIFT to `9d282e415c5e3b0f65b82c8758a013b269583d93` and EALE to `419789ed1662e7b9ad16aa6620a4b31449612948`. The dataset has 56 training tasks and 28 test tasks.

Within each scene, q1/q2/q3 supply training trajectories and evolution evidence. q4 validates candidates; only permitted aggregate metrics reach the proposer. Finish and freeze every scene selection before executing q5/q6 across A (minimal ReAct), B (the same Agent with K0), and C (selected K1). Rejected candidates explicitly fall back to K0, with a separate C execution.

Required coverage is 56 training/validation cells plus 84 A/B/C test cells: 140 total. Candidate admission and extra validation/evolution requests are additional costs. All arms share the model and primitive permissions, with at most 30 native interaction rounds per task. Keep this protocol separate from the historical three-round sales diagnostic.

Each execution or correction episode has a limit of 64 expanded primitive calls, shared by all arms. No fixed model-request cap does not imply unlimited tool calls. The current adapter also stops after eight consecutive action-protocol failures; a valid action resets this consecutive count. Invalid JSON and unregistered-tool rejections are recorded, and a stopped episode remains a failure. All model costs are retained even when a rejected reply executes no tool. These conditions are identical across arms.

The frozen kernel registers eight primitives: list files, read text, read tables, calculate over tables, write files, replace a text fragment, search the web, and read web pages. `replace_text` requires the latest file SHA-256 and exactly one matching fragment; stale files or ambiguous matches are rejected without writing. It is one registered write primitive shared by all arms, with one expanded primitive call counted per dispatched call, rather than an F-composition gain. Actions rejected by checks before dispatch have no actual primitive call; check rejections are recorded separately. Calls to registered tools inside F compositions are recorded and counted separately. Web tools do not execute JavaScript or arbitrary host code. Tasks requiring unsupported capabilities may fail and remain in the complete matrix.

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

## Historical development snapshot

As of 2026-10-13 02:11 China Standard Time, the private frozen candidate `minimax-full-v030-005` has completed 1/140 required cells and is executing q2. Its first training task achieved native success and content score 1.0, with a completed trajectory evaluation scoring 0.92. Ten interaction rounds took approximately 15 minutes and used 88 actual model requests, 45 expanded primitive calls, and 3014945 prompt/completion tokens across execution and both judge roles. Original records and the cumulative shared ledger remain preserved.

This is private candidate training evidence, not a released-package result, an A/B/C comparison, or full acceptance. The three-arm holdout and q6 remain incomplete. Extended core regression still has two outstanding metadata and behavior-contract items, and installed-package acceptance is pending. See the [implementation record](../superpowers/plans/2026-10-12-minimax-full-lift.md) for decisions, rework costs, and retained historical results.

With Langfuse disabled, native LIFT `tool_calls` is null for this task and remains unchanged. A separate read-only audit of the current JSON ReAct adapter reconstructs 45 Agent-level invocations and 45 expanded primitive calls from the complete verified model/observation chronology. This is derived accounting, not recovered native telemetry or a claim about every external Agent loop. A macro counts as one Agent invocation, with its primitives counted separately. Known failures and check rejections remain included, unexecuted batch entries are excluded, and missing observations remain unknown. Seven counterexample tests and independent review passed, with no extra model calls or changes to frozen runtime sources.

### Subsequent diagnosis (2026-10-13 02:45 China Standard Time)

In the same frozen run, q2 achieved native content success and score 1.0 after 14 rounds, but its trajectory judge returned HTTP 400. Its main cell therefore remains failed and unhealthy. The provider error body was not retained, so the exact cause is unconfirmed. A roughly three-million-character judge input supports a size hypothesis, not proof of context overflow. Complete worker trajectory verification passed; a separate audit found 69 Agent invocations and 69 expanded primitive calls, with no unconfirmed actions. Scheduling continued to q3. Original failure records and cumulative costs remain intact, without rejudging or score stitching.

An offline candidate preserves only fixed error categories before sanitization, retaining no free-form error text, request bodies, or credentials. This improves diagnosis and does not establish a fix for HTTP 400. The active run has not adopted this candidate; full acceptance remains incomplete.

### q3 terminal outcome (2026-10-13 02:52 China Standard Time)

Each execution or correction episode has a fixed limit of 64 expanded primitive tool calls, shared by A/B/C. Unbounded model calls do not imply unbounded tool calls. q3 exhausted this tool limit during its eighth execution episode and remains a failed main cell. Whole-task execution accounting records 76 model calls and 87 primitive calls, with zero unknown or pending operations. The final episode included 57 web reads and 26 known web failures. Scheduling continued to q4. No final native content assessment completed; the previous partial score cannot serve as a terminal score.

### Stopped run and isolated diagnosis (2026-10-13 03:13 China Standard Time)

`minimax-full-v030-005` exited safely at a boundary with confirmed request and tool receipts. Its status is superseded-incomplete; no benchmark executor from this run is currently live. The cumulative count at exit was 2262 actual requests. Original failures, partial q4 trajectories, and the ledger remain preserved. These scores will not establish acceptance for a later frozen version.

Isolated diagnosis `minimax-http400-diagnostic-001` sent the original q2 judge input once and received a confirmed HTTP 400 again, bringing the cumulative count to 2263. It performed no tool actions and introduced no unknown request. The fixed error classifier did not identify a specific cause; neither quota exhaustion nor context overflow is established. A/B/C and q6 remain incomplete, and acceptance has not passed. Safe fixed error signatures will be added before further diagnosis and a fresh frozen full run.

### Context-limit cause confirmed (2026-10-13 03:22 China Standard Time)

Isolated diagnosis `minimax-http400-diagnostic-002` sent the same original q2 judge input. A fixed complete provider error signature explicitly identified context_limit. The cumulative actual count became 2264 with no new unknown request. The previous length hypothesis now has provider-signature evidence; neither the error body nor credentials were persisted.

A new private candidate represents large duplicate observations following tool receipts using explicit references. The first complete tool output, all decisions, failures, checks, ordering, and the immutable raw transcript remain retained. Messages containing floats, duplicate JSON keys, nonfinite numbers, different composite outputs, or ambiguous tool IDs are left unchanged. Seventeen q2 references reduce the judge input from 3000082 to 1994416 characters. Seven new focused tests, four original-rubric binding tests, and 101 LIFT regressions passed. Initial review findings concerning numeric precision and rubric binding, along with repair logs, remain preserved.

This is a new judge-input representation protocol. It retains the native rubric and score parser but differs from the unmodified native input. It does not establish judge-quality equivalence, resolution of token limits, or full acceptance. A fresh frozen full experiment must apply the same protocol to all arms and rerun them without mixing old scores.

### One-input real retest (2026-10-13 03:39 China Standard Time)

`minimax-http400-diagnostic-003` used the same original q2 transcript and scoring requirements with the declared reference representation. It received a complete response and passed strict native JSON score validation. The trajectory score was 0.54 and remains a diagnostic result, excluded from old cells, candidate selection and A/B/C scores. Usage was 752341 input tokens and 10013 output tokens. One actual request raised the cumulative count to 2265, with no new unknown request or task tool action. This establishes successful judging for this input, not resolution for every long transcript or judge-quality equivalence.

### Fresh frozen full run (2026-10-13 03:45 China Standard Time)

`minimax-full-v030-006` started from the original cumulative ledger at 2265 requests. Process liveness was verified, all 140 main cells were registered, and first-task q1 training began. The fresh identity uses the same model and permissions, complete raw trajectories, and the declared judge representation for all arms; old scores are not imported. Its first actual worker request includes used/remaining model and tool budgets; the per-episode primitive limit remains 64. Full coverage, A/B/C and q6 are incomplete, acceptance remains false, and existing compatibility/package gates remain pending.

### Rate limiting and recovery repair (2026-10-13)

`minimax-full-v030-006` exited safely at 2289 cumulative actual requests. Its 24 new attempts comprise four completed responses and twenty confirmed HTTP 429 failures, with no new unknown requests. Original receipts and failed cells remain retained. The user reported restored MiniMax quota; live availability still requires a guarded request.

The new private candidate retries only confirmed HTTP 429, with at most three actual attempts per logical request. Each attempt has its own original receipt and charge on the same ledger. All roles share durable 60/120/240-second backoff, capped at 300 seconds during consecutive rejections. A pre-dispatch intent and immutable receipt-bound events cover both crash windows; unknown responses never retry automatically. All 108 offline checks passed. A fresh frozen full identity still awaits verified launch; A/B/C and q6 remain incomplete and acceptance is false. Reports must distinguish logical model steps from actual HTTP attempts.

The guarded real availability check returned confirmed HTTP 429 on all three attempts, including shared backoff waits. Three charged calls raised the cumulative ledger to 2292, with no new unknown request. The check process has exited and full run 007 has not started. The specific quota dimension or provider restriction remains unconfirmed; reported quota restoration does not replace observed API evidence.

Supplemental offline installation and compatibility validation: all 119 frozen private core files exactly match both the wheel and installed directory. Installed examples, doctor, replay and resume passed outside the checkout with zero model requests. Using the installed core from the correct repository test directory, 685 of 693 framework checks passed with eight existing external-resource skips; all 18 LoCoMo and 11 TravelPlanner checks passed. The initial wrong-working-directory failure log remains retained. The outdated unregistered-tool test now follows the existing before-effect rejection contract and adds zero-tool-call, budget and replay assertions, independently reviewed. Frozen runtime sources were unchanged. Dependencies were reused rather than installed fresh, private version metadata was unchanged, and no release occurred. Real A/B/C and q6 remain incomplete; overall acceptance is still false.

2026-10-13 06:40 update: the MiniMax 008 executor remains live. Of 140 main cells, 7 are completed, 1 failed, and 132 have not started; evolution after training is underway and the three-arm holdout has not started. The isolated diagnostic launcher now binds cached results to actual request receipts, rebuilt input and model/source identity, and reconciles all physical request costs exactly with the original HTTP ledger. The 17 cache, 14 dispatch and 5 input checks passed, as did the independent code review. Diagnostic readiness has not been established and no new diagnostic request was dispatched. Original records and budgets were not reset. This is offline repair evidence, not three-arm or q6 acceptance. Evidence: `infra/full-training-diagnostics/174-diagnostic004-receipt-cache-cost-binding-review.json` within the experiment directory.

2026-10-13 07:01 training diagnostic: the first 009 task remains live. Its first three native content scores were 0.46, 0.50 and 0.54, all below acceptance. Initial feedback identified ungrounded product/price recommendations; later feedback concerned missing PPT fundamentals. Confirmed web challenges, blocks and network failures retain tool receipts. No new unknown model request has appeared. Intermediate scores are not final results, and three-arm holdout has not started. Evidence: `infra/full-training-diagnostics/187-*` and `188-*`.
