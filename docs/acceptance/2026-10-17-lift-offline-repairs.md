# LIFT offline repairs and workspace integration — 2026-10-17

This record verifies implementation, compatibility and packaging. **It does not establish completion of the real benchmark or improved evolutionary performance.** The running v013 experiment retains its frozen source; scores from different versions are never combined.

## Integrated changes

- Oversized macro results produce a structured failure with completed primitive receipt references, allowing ReAct to recover. Restart replay preserves completed writes.
- Unknown writes, cancellation and unreceipted timeouts remain gated. Checker and graph-view exceptions propagate as failures instead of being converted into recoverable macro errors.
- Previously reviewed recovery, cost-accounting, tool-reading, delivery-coverage, bounded scalar-retry and calendar-replay changes are synchronized while retaining original evidence and historical assets.
- Shared fixture builders follow the contribution rules: scenarios do not import TestCase classes from other scenarios, and support modules do not depend on unittest.
- Historical recovery proof uses one directory traversal and one read per step file, retaining raw-byte hashes, prior text decoding, and rejection of symlinks, unknown operations and unresolved trace errors. It does not trust timestamp caches.

## Verification

| Check | Result |
|---|---|
| Framework regression | 762 tests; 8 existing resource-dependent skips; all others pass |
| Offline LIFT | 164 pass, including historical recovery proof and nested reference cases |
| LoCoMo | 18 pass |
| TravelPlanner | 11 pass |
| Installed wheel | Passes outside checkout, including 9 additional recovery tests |
| Static checks | Ruff and whitespace checks pass |
| Wheel source parity | 112 framework Python files match the workspace byte for byte |

Workspace regression uses Python 3.14; installed-wheel acceptance uses Python 3.12. LIFT tests explicitly configure the pinned upstream before discovery. Compatibility imports use json-repair 0.64.0 and pypinyin 0.55.0. Docker and Langfuse are not started. Tests use recorded responses, mock HTTP or local test servers, with no real model requests.

The original eight-read failure was replayed offline: the former 180 KB aggregate-result failure becomes recoverable and all eight primitive receipts remain counted. This does not prove real-model recovery.

A single component diagnostic covers 4 historical runs and 1,494 evidence files (67,961,807 bytes): approximately 1.64 seconds before and 0.64 seconds after, with proof identical to the original record. This is not an overall benchmark performance result. The running frozen v013 source does not adopt the optimization. Evidence: local records 360 and 362, plus `local-proof-full-lift-regression-v002.log`.

## Nested reference mode remains disabled

The explicitly enabled v4 judge-input mode references exact earlier tool outputs inside composite observations. Existing v1/v2/v3 reading and verification remain supported. Only host-registered paths are expanded; similar markers in original data remain literal. Noncanonical JSON, floating-point values and objects with different key order remain unchanged. The key-order compatibility case was verified failing before its repair and passing afterward.

Both actual context-limit traces pass offline reconstruction and input verification without changing their original files. Complete judge messages shrink from 3,385,388 to 2,756,101 characters and from 3,494,704 to 2,533,095 characters, approximately 18.6% and 27.5%. This does not establish context-limit compliance or real scoring equivalence, so neither `full_episode` nor the live benchmark enables the mode. Evidence: local record 366, `nested-native-green-v002.log` and `nested-projection-full-lift-v001.log`. This offline validation makes no model calls.

## Outstanding work

- Full real A/B/C, q6 and quality acceptance remain incomplete. Adoption of changed source requires a fresh frozen experiment identity and new runs for all three arms.
- Two native trajectory scores failed with confirmed context-limit errors. Their losslessly deduplicated inputs still contain roughly 3.4 million characters. No verified solution or imputed score is claimed.
- Record 364 identified 132 and 111 nested duplicates missed by the existing projection. The v4 mode above now passes offline reconstruction and marker-ambiguity checks; real scoring behavior and context-limit compliance remain unverified. Offline compression is not acceptance evidence.
- Additional portability verification: without the optional upstream, the complete ordinary suite runs 762 tests with 22 explicit skips (including the existing 8 resource-related skips), and all remaining tests pass. With the pinned upstream configured, all 27 targeted cases execute and pass. No host-specific upstream path is required.
- No commit, push, package publication or upstream promotional message was performed.

## Disk exhaustion during the real run

At approximately 20:19 local time, v013 exited with `ENOSPC` during the thirteenth native turn of the research-platform task. The cumulative ledger remains at 8,688. New request `505b46efcb004987b03ee8ac580e35ef` and its model step retain their submitted state; a complete result cannot be confirmed. No retry, abandonment, budget reset or completion claim was made. Recovery requires the maintainer's separate authorization for this new unknown request.

Native filesystem transparent compression released 2,781,081,600 bytes (approximately 2.59 GiB) across 8,249 evidence files. Raw-byte hashes, permissions, ownership and modification time were checked for every file; a second verification confirmed all hashes, last-request evidence and frozen source remain unchanged. Paths and JSON formats remain readable as before. The approximately 45 MB rebuildable wheel smoke-test environment was removed; the wheel and acceptance logs remain. Evidence: local records 369–372. This recovery makes no model calls and does not complete benchmark acceptance.

A combined resource supervisor is prepared but not started, replacing the earlier quota watcher. Dispatch requires at least 2 GiB free; below 1 GiB it suspends the same process only at a boundary with no unsettled model, tool or physical-request intent. Resume requires both sufficient storage and expiry of the five-hour quota wait. It must not run alongside the earlier watcher. Nine offline safety cases and a limited review pass, including a new 429 after suspension, the resume counter boundary and failed event logging. No real signals or model requests were sent. Evidence: record 373. The new unknown request still awaits authorization; recovery plan 374 has not been applied.

Local evidence is retained under `.superpowers/sdd/2026-10-09-agent-runtime/infra/`: integration manifests 347–348, repair evidence 338–343, request-size diagnosis 341/345/346, and `workspace-integration-*` / `workspace-wheel-*` logs. Original failures and the cumulative ledger remain preserved.
