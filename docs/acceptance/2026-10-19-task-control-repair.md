# Cross-interaction budget feedback and retrieval convergence repair

[简体中文](2026-10-19-task-control-repair.zh-CN.md)

**Status: budget-feedback, retrieval and recovery mechanism checks passed. The native task failed content acceptance (0.75); trajectory score was 0.92. Full evaluation and q6 remain unstarted, and the overall goal is incomplete.**

The previous trial exhausted 24 execution requests during its second interaction, after 38 web-read/search calls without corrective delivery. Original results and accounting remain retained in local proof `405`.

The opt-in `bounded-task-v2` policy preserves the old `bounded-task-v1` declaration and defaults. Each execution request records actual task-wide used/remaining requests before `AgentContext` validates budgets and replay inputs. Historical replay reuses its saved snapshot; only new steps read current counts. Evaluator requests are excluded from the execution quota. Projection policy binds execution identity, and native trajectories retain the model-visible snapshots.

Host tool handlers limit combined web reads/searches to 12 across the task, disable a retrieval tool after three known failures, and stop external retrieval when at most four execution requests remain. Healthy retrieval tools remain available when only another tool fails. File reading, editing and delivery remain available. Composite functions cannot bypass these handlers. Rejections occur before network effects, remain known failure receipts, and still count as primitive attempts. Missing verification must be reported honestly; native evaluation is unchanged.

This repairs execution control without evolving a K1 or proving overall S/F/C/P gains. The added restrictions must be disclosed; fewer tool calls alone do not establish improved quality or efficiency.

Offline validation passed 206 LIFT tests, 762 framework tests with eight existing explicit skips, 18 LoCoMo tests and 11 TravelPlanner tests, plus Ruff lint/format checks. Tests cover cross-interaction counts, evaluator separation, immutable replay, macro restrictions, network-free receipt replay, complete native scoring and trajectory extraction. They made no real model calls and do not replace quality or new-package acceptance.

The real trial retains the same PPT training task, fixed MiniMax model/endpoint, 24 execution requests and three interactions, with at most 40 actual requests across all roles. The cumulative ledger starts at 8,822 and must stop by 8,862. Other tasks, the full matrix and q6 remain excluded.

Initial dispatch stopped after four settled model responses because free space fell below 2 GiB. No new unknown request was created. Recovery verified restored space, complete receipts and a known unsent next model step. Settled responses replay without refunding counters or repeating tools.

Original startup records, the resource-stop log, paused summary and receipt fingerprints remain preserved. Recovery explicitly excludes inactive storage waiting from the 45-minute active-runtime allowance: about 506 seconds were consumed and 2,194 remain. This operational clock amendment does not reset request accounting. Final reporting must distinguish wall time, inactive waiting and active time; this cross-day recovery cannot support a speedup claim.

Local proof `410` binds source/configuration, `411` records initial dispatch, `412` pins resource-stop receipts and settled steps, and `413` records recovery dispatch. Final audit is retained in local proof `414`.

## Final real result

The trial made 34 actual requests: 21 execution, 12 content-evaluation and one trajectory-evaluation request, across three interactions. Native success remained false with final content score 0.75; complete trajectory evaluation scored 0.92. Worker primitives totaled 20, including two provider challenges. External retrieval used three web reads and four searches. Model input/completion tokens totaled 675,020/59,019. Immutable snapshots showed task requests used 0–20 and remaining 24–4 without a cross-interaction reset.

The ledger advanced from 8,822 to 8,856. Thirty requests followed recovery; the original four were retained rather than sent again. All four original model receipts and eight settled tool records kept their hashes. There were no new unknown requests. The process exited and released its emergency reserve. Six all-role requests and three execution requests remained unused; no extra attempt was added to spend the allowance.

Active time was about 33m05s including time before the pause. Task wall time including inactive storage waiting was about 38h59m53s and cannot support a speed comparison. The recovery process peaked around 242 MiB, below the 640 MiB limit.

Content feedback progressed through 0.67, 0.71 and 0.75, identifying missing software categories, page categories and finally line-spacing guidance. The task ended normally at three interactions. Its strict `regression_passed` quality field stays false; proof `414` separately records passed mechanism checks. Neither relabeling task failure nor adding a fourth interaction is permitted.

The earlier 24-execution-request trial stopped during its second interaction without final content or trajectory evaluation. Its 38 retrieval calls versus this trial's seven are diagnostic observations, not an isolated causal estimate or proof of general evolution gains. Extra evaluator calls also count toward total cost; reporting only fewer execution requests would be misleading.

Offline export `415-public-q1-training-evidence.json` contains only the q1 public instruction, two feedback messages actually delivered to the Agent, and summary metrics. It excludes the undisclosed final judge reason, private requirements, validation records, q5 and q6. Exporting it called no model and neither proposed nor adopted a kernel.

The next question is whether valid failure evidence drives S/F/C/P changes that improve independent-task results. This one training-task repair regression does not replace full A/B/C evaluation, q6 confirmation, generalization evidence or release acceptance. No automatic budget expansion or full dispatch follows.


## Offline evolution check for failed training samples (2026-10-20)

Added `test_completed_content_failures_reach_evolution_without_validation_feedback`. A recorded client completes training and validation tasks with unsuccessful content judgments under the same 24-worker-request, three-interaction policy and retrieval controls. Training feedback reaches Wiki requests and the proposer runs. Private validation feedback and hidden content/trajectory references stay out of learning requests. A recorded no-change proposal retains K0.

Network connections are forbidden in this check; actual model requests are zero and the cumulative ledger remains 8,856. The test passes, with evidence in `infra/task-control-failed-training-offline-v001.log`; the related test file passes Ruff checks. This verifies that complete unsuccessful samples remain eligible for learning and that validation information stays isolated. It does not import the real q1 export into Wiki, generate a real candidate, or demonstrate improved S/F/C/P quality. Candidate count and independent validation must remain bounded before considering a full benchmark.


### Real public training evidence imported into Wiki

Registered the reviewed real q1 public evidence export (`415`) in the separate `minimax-public-training-wiki-001`, recorded it through `WikiMaintainer`, and retrieved both observed user feedback messages through the raw `WikiService` query. Duplicate imports and records remain idempotent; source bytes and the cumulative ledger remain unchanged. The reproducible `infra/import-public-training-offline-v001.py` forbids network connections and model-client construction. Evidence: `416-public-training-wiki-import.json`; additional actual model requests: zero.

This is training material for a future proposal, not a resumed formal campaign. It contains public feedback and metrics, rather than a complete execution trace. No real proposer has run, no candidate has been generated, and independent validation remains outstanding. This does not establish evolution success or full benchmark acceptance.


## Bounded real candidate proposal (2026-10-21)

The user explicitly authorized at most two sends from the remaining allowance, on the same ledger. The first send queried the public q1 Wiki original; the second submitted one candidate. Both receipts completed, with no new unknown requests. Usage: 7,471 prompt tokens and 2,054 completion tokens; approximately 106 seconds active elapsed. Ledger: 8,856 → 8,858. No training rerun, Wiki maintenance-model calls, validation, q5 or q6. Authorization/result evidence: `418` and `419`.

Candidate `a7849b3dd2ef441f6c1317e26789551390c8049b5938f2c1ab4829a26c934b06` changes P only, adding requirement coverage, alternatives, complete structure and a pre-delivery omission check. Static asset, template-slot and restricted-code validation passed; S/F/C are unchanged. The candidate is not adopted. Static validity establishes neither quality improvement nor S/F/C evolution.

Diff review identified unresolved risks: the candidate removes K0's explicit metric-definition and source-value computation instruction, requiring a tabular-task regression check before adoption. Page-structure wording follows PPT feedback and needs an independent overfitting check. The public evidence package lacks a complete execution trace; this proposal does not establish full trajectory-driven learning. Behavioral admission, independent selection and the complete benchmark remain outstanding. Both authorized sends are consumed; no automatic additional requests.
