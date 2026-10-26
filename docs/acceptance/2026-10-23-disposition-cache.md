# Reduce repeated reads during historical model-disposition validation

The running q2 baseline/candidate pair continues to use `full-resource-candidate-v004`. This change affects the working checkout only; the frozen executor is not modified or restarted and results will not be mixed across sources.

## Reproduction and cause

Recent completed model responses took approximately 4–8 seconds while request-start gaps were approximately 43–70 seconds. Separate read-only measurements found source inventory hashing at 0.07 seconds and parsing 8,863 receipts at 0.31 seconds; neither alone explains these gaps. Profiling one historical disposition check took 4.57 seconds, including 3.69 seconds inspecting its complete local run. The overall check raised ValueError because a live request was in flight; this was neither an authorized disposition nor evidence that recovery failed.

The local check repeatedly decoded all historical steps to reject unfinished tool operations. Four historical unknown requests have explicit disposition records, amplifying repeated inspection cost. This identifies one component, not a complete end-to-end latency attribution.

## Change and boundaries

Cache only step kind/state fields in process memory, capped at 32,768 entries. Model inputs, tool outputs and judge content are not cached. Directory enumeration and symlink checks remain live. Device, inode, size, mtime_ns and ctime_ns identify file versions; metadata is rechecked after access. Original receipt hashes, ledger reconciliation, request identity, local session binding and unknown-tool rejection remain in place.

On the same real historical scope, the old implementation took 3.38 seconds, the new cold inspection 3.23 seconds and a warm inspection 0.49 seconds, returning identical scope identities. This measures only local scope inspection, not task-level speed or evolution quality.

## Verification

The new repeated-read test fails as expected against frozen old code. All three new tests pass against the change, including same-size edits with restored mtime, new unfinished tools and symlinks. Core: 765 total, 757 passed and eight explicit external-evidence/dependency skips. Fourteen additional initial skips were rerun successfully with pinned LIFT configured; the initial follow-up loader naming error is retained and v002 is the valid follow-up. LIFT: 207 passed. Related Ruff checks pass. Offline checks use zero model requests.

Evidence: `423`–`426` and referenced logs. New source `full-resource-candidate-v005` is frozen (332 Python files), with the old snapshot unchanged. Three cache contract checks pass against the frozen source; evidence is `427` and `disposition-cache-frozen-contract-v001.log`. This repair has not had a live regression. Later evaluations must use a new identity; current paired results cannot be presented as results of the new source. Full A/B/C, q6 and the overall goal remain incomplete.
