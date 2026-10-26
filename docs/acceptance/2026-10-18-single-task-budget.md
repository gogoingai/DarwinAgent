# MiniMax single-task execution-budget verification

[简体中文](2026-10-18-single-task-budget.zh-CN.md)

**Result: failed. Neither the full campaign nor q6 was started.**

Only the previously failed PPT beginner-guide training task `scene-a1dff4d546a0a466-q1` was rerun. The task, native evaluator, K0, fixed MiniMax model and execution source were retained. A new workspace used at most 24 execution requests across three interactions, with a separate hard cap of 40 actual requests across execution and evaluation, plus pre-dispatch storage and memory protection. Historical records and the cumulative ledger remain preserved.

| Measurement | Result |
|---|---:|
| Actual model requests | 27: execution 24, content evaluation 3 |
| Input tokens | 818,056 |
| Completion tokens, including reasoning | 53,212 |
| Native task execution time | About 25 minutes 29 seconds |
| Primitive tool calls | 48 |
| Known tool failures | 12: web reads 7, searches 3, text edits 2 |
| New unknown model requests | 0 |
| Cumulative ledger | 8,795 → 8,822 |

The first interaction produced HTML. Content evaluation returned `success=false, score=0.625`, requesting named, verifiable resources, links and prices. This is intermediate feedback, not a final content score. The second interaction stopped at the cumulative 24-request execution cap, before its final delivery, final native content result or trajectory evaluation.

Tool records contain 33 web reads, five searches, two file listings, three text reads, four text replacements and one file write. The 33 web reads requested distinct URLs; they are not repeated retries of one URL. Access failures, empty responses and client-block pages remain in the original receipts. Both text-edit failures report `edit_match_not_unique`.

## Findings and limits

- Raising execution capacity to 24 requests still did not complete this task. This does not justify further automatic budget increases or a framework-improvement claim.
- External-resource discovery consumed many second-interaction calls without delivery before exhaustion. Search convergence and the transition to writing/delivery deserve investigation; no effective repair was demonstrated here.
- Source and retained model inputs show that visible budgets expose per-interaction execution counts, while transport separately enforces the cumulative task cap. An explicit cross-interaction remaining-request value is missing. A repair must preserve deterministic recovery rather than rebuilding old request inputs from the latest ledger.
- The full-scene K0 still reuses initial assets referring to metric definitions, input tables and calculation from source values. This establishes an analytical-task bias in its initial prompt; its effect on PPT results has not been isolated.
- Different budgets elicited different intermediate feedback. Without paired final evaluations, the 0.625 intermediate score cannot establish improvement over the old intermediate scores.

This is one K0 task-budget feasibility check, not an A/B/C comparison, evolution run or independent q6 confirmation. Full acceptance remains incomplete. There is no automatic retry or scope expansion. Budget feedback, search and delivery control should first be repaired offline, then checked with a bounded test under a fresh frozen identity; those repairs are outstanding work.

Original outputs remain under local `minimax-task-budget-feasibility-001/`. Record `403` binds source/configuration, `404` records dispatch, and `405-single-task-24-request-real-result.json` retains all 27 request fingerprints, tool statistics and failure indexes. These local artifacts are not published with the source; public conclusions require sanitized evidence.
