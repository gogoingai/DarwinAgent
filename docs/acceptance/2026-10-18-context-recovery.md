# LIFT context and recovery diagnostic

[简体中文](2026-10-18-context-recovery.zh-CN.md)

**Status: the real context diagnostic passed for one previously failing large trace. The full three-arm evaluation and q6 confirmation remain incomplete. This is neither a benchmark effectiveness report nor release acceptance.**

## Fixed conditions and cost

Every real request used `MiniMax-M3.1-Flash-Preview`, `https://api.minimax.cn/v1`, temperature 0, adaptive thinking, reasoning effort max, and a 16,384-token completion cap. The context diagnostic advanced the original ledger from 8,688 to 8,693. All five reserved requests, including the failed judgment, remain charged; no budget was reset or refunded.

| Diagnostic | Input tokens | Completion tokens, including reasoning | Outcome |
|---|---:|---:|---|
| Small trace, original representation | 1,997 | 97 | Completed score: 1.0 |
| Same small trace, typed references | 2,176 | 11 | Completed score: 1.0 |
| Historical large trace, typed references | 576,010 | 16,384 | Settled response with empty final content; invalid judgment |
| Same small trace, readable references | 1,951 | 67 | Completed score: 1.0 |
| Same historical large trace, readable references | 534,843 | 13,535 | Completed score: 0.62 |

Total cost was 1,116,977 input tokens and 30,094 completion tokens. There were no new unknown requests and no automatic resubmission of the failed large input. The 0.62 score diagnoses one historical trace; it is not an A/B/C result or evidence of improved framework capability. A single representation comparison cannot establish general equivalence or reliable gains.

## Changes and evidence

- Referenced trajectory inventories retain every message, primitive tool call, and source fingerprint without embedding full original model receipts and step snapshots repeatedly. Original files remain unchanged.
- `full-lift-judge-input-v6` retains the native system prompt and scoring requirements while displaying readable JSON fields and host references. Its `value_refs` are identical to v5 and decode to every original content byte. References do not count as tool calls.
- Optional resource protection checks disk and process memory before dispatch and reserves exclusive emergency receipt space. An offline injected disk-full failure releases that space and retries only the local write. It does not request the received response again; subsequent model dispatch is blocked.
- A new optional retry policy stops on empty final content at the fixed completion-token cap, retaining the failure and all costs. Historical v1 retry rules and artifacts retain their original interpretation.
- An independent process replayed the completed real small-trace judgment with the same score and zero additional model requests. Real ReAct recovery after a file write also passed: the first request produced a settled write receipt, then a controlled interruption stopped at a known unsent model boundary. An independent process sent only the final response request. The write receipt, artifact hash and modification time stayed identical; terminal replay added zero requests. This does not authorize retrying a write with unknown outcome.

The frozen diagnostic source passed 183 offline LIFT tests. After adopting the readable display in the complete execution path, focused checks cover matrix execution, durable replay, and compatibility through the thirtieth native round. Real inputs bind independent source/configuration identities. Historical v013 and both frozen diagnostic sources remain unchanged.

Holding both representations at once peaked around 793 MiB, exceeding the diagnostic limit; that approach was not used for real requests. A cold process checking one larger input peaked around 451 MiB. Real requests peaked around 530 MiB, below the 640 MiB limit. All emergency reserve files were released; original evidence remains retained.

## Full execution configuration and remaining acceptance

Full execution uses the originally approved three interactions and eight execution requests per task, frozen as `bounded-task-v1`. With three interactions and eight execution requests, the cap accumulates across correction turns and process recovery for the same task. Reaching it creates no new send intent and does not block other tasks. The execution cap counts actual HTTP receipts. Known resource rejections before dispatch remain recorded without consuming an actual request slot. The cumulative ledger is unchanged. Execution and native content evaluation send a confirmed HTTP rejection only once; Wiki, proposal and trajectory roles retain their declared retry policies, with every actual send charged.

Offline checks retain all 140 required cells, including 84 A/B/C holdout cells, while preserving the legacy thirty-round mode. The new option binds the run identity, Agent declaration, and final report. Reports use `full-lift-local-bounded-v1`, preventing silent comparison with the old configuration. The eight-request cap applies only to the execution Agent. Evaluation, Wiki, proposals, and other costs still enter the same ledger: eight times the task count is not an upper bound on the complete experiment's total requests.

The latest offline LIFT regression passed all 199 tests. The new execution identity retains every task and arm. All scenes complete q5 A/B/C diagnosis before any q6 dispatch. Only complete, healthy q5 execution and judgment unlock q6. Low content quality is distinguished from execution or evaluation faults; scoring rules remain fixed.

The separate real write-recovery test used two requests, advancing the ledger from 8,693 to 8,695. Context and recovery together added seven requests, with 1,118,876 input tokens and 30,212 completion tokens, and no new unknown requests. Framework regression passed 762 tests with eight existing explicit skips; LoCoMo passed 18 and TravelPlanner 11. Independent wheel installation passed, with exact source parity for 113 core modules. These checks do not replace the full three-arm matrix, independent q6 confirmation, or reporting every failure and regression.

Raw receipts, inputs, the ledger, and stepwise proofs remain in the local acceptance directory; key records are `375`, `378`–`385`, `389`, and `392`–`393`. They are not distributed with the source. Public conclusions require an exported, sanitized reproduction bundle.

## Execution-budget diagnosis after full startup

The first eight training/validation tasks across two scenes all terminated under the cumulative eight-execution-request cap. Available content scores are intermediate-turn feedback, not final scores. Native execution had not returned a complete result, so final content and trajectory judgments are missing. The unhealthy flag therefore reflects incomplete evidence; there is no basis to fix this by relabeling records, inventing final scores, or weakening candidate admission.

To avoid repeating the same budget truncation across the remaining tasks, dispatch is held at a verified boundary with no in-flight model request or unsettled tool operation. Original source, records and the ledger remain preserved. This full startup sent 100 requests, advancing the ledger from 8,695 to 8,795. A one-task, three-round verification with at most 24 execution requests has been proposed; the execution-budget choice is pending. q6 remains unused and three-arm acceptance incomplete. Local proofs `397`–`398` are diagnostic evidence, not evidence of framework improvement.

## Authorized 24-request single-task result

After user confirmation, only one failed training task was rerun and remained incomplete: 24 execution requests and three content-evaluation requests, 27 actual sends in total. The ledger reached 8,822 with no new unknown requests. Resource discovery exhausted the second interaction before final content or trajectory scoring. See the [single-task report](2026-10-18-single-task-budget.md). Full execution was not automatically resumed; q6 remains unused.
