# Authorized recovery of the q2 pair

The user authorized abandoning the unknown result of request `232acc5298a048828226391eaee05d2d` and resuming its model step. Original submitted receipt, service intent and step bytes are archived. The finalized unknown receipt retains its one-send charge. The ledger stayed at 8,871 through disposition and reached 8,872 after a complete replacement response. Old GLM requests remain untouched.

## Recovery boundary

The original preparation snapshot says nine worker sends used and 15 remaining. Charging the unknown send makes the actual counters ten used and 14 remaining. The normal controlled executor rejects that stale snapshot. This recovery uses an explicitly authorized host-only single-model entry point: preserve the exact model input and original preparation snapshot, independently enforce the actual worker cap of 24, and dispatch through the same shared transport and ledger. It does not relabel the original snapshot as current. The entry point permits one exact-input call and executes no tools. Subsequent new logical model steps still receive current counters.

The complete replacement response is saved in the authorized retry journal. All 28 previously completed step hashes and all frozen source hashes are unchanged; continuation replays these records instead of repeating their tool operations. This is an audited operational recovery, not a generic fix of TaskWorkerSession's exact-retry/budget-snapshot compatibility boundary; that boundary still needs a general regression repair.

## Environment and runtime clock

The temporary upstream checkout was partially pruned again. Its 301 tracked files and Git metadata were restored from the stable local backup at the identical revision `9d282e415c5e3b0f65b82c8758a013b269583d93`. Prior metadata and differing files remain archived. All frozen Python hashes match; no upstream version changed.

The continuation executor starts in an independent session to reduce interruption coupling. Original timestamps remain intact. Pre-interruption active time cannot be reconstructed precisely, so the additional operational timeout allowance is explicitly recorded, rather than pretending to preserve the original 45-minute active clock. The pair is ineligible for a fair latency/speed comparison. Send ceilings remain unchanged: original K0 cumulative ceiling 8,898, pair ceiling 8,938, at most 24 worker sends and three interactions per task.

Evidence: `428` interruption snapshot, `429` authorized disposition/retry preparation, `430` pinned upstream restoration, `431` detached dispatch and `432` complete replacement response with historical steps preserved. Final native scores for both arms, independent quality effects, full A/B/C, q6 and overall acceptance remain incomplete. The candidate is not adopted.
