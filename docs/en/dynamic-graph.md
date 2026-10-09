# Dynamic graphs and asset change signals

New LoCoMo g1 runs use an LLM to construct graphs under the current S, enable Wiki optimization, and allow S/F/C/P revisions by default. Frozen facts, fact IDs and the vector index are retained. Other entities, properties and relations are generated from those facts and their original sources, without a fixed person/topic/session vocabulary.

S declares types, properties and relations; P.extract guides construction. Every generated node and edge must cite a fact in the current batch, a source registered for that fact, and a verbatim quotation. Fixed checks enforce types, keys, endpoints, provenance and instance constraints. The model cannot rewrite atomic facts or create unsupported endpoints.

Graphs are persisted under an identity covering facts, sources, S, P.extract, execution configuration, model connection identity and builder implementation. S or P.extract changes rebuild the graph; F, C and other P changes reuse it. Admission, trials, generation and held-out campaign evaluation share the builder. Model receipts persist before graph completion; unknown submitted requests are not silently retried after restart.

Training feedback exposes active stages, graph statistics, unmaterialized declarations, isolated facts, graph fingerprints and asset change signals. A portion of the feedback budget is reserved for graph evidence. These are hypotheses requiring evidence, not automatic causal verdicts:

| Asset | Observable signals | Check before proposing |
| --- | --- | --- |
| S | Missing structure, unmaterialized declarations, graph type errors | Whether the source supports the structure and S can represent it |
| F | Parameter/execution errors, empty retrieval, missing or truncated traversal | Whether information exists in the graph and the tool retrieves it |
| C | Rejections or input/contract mismatches | Whether the rejection is correct or the check is demonstrably wrong |
| P | Construction, retrieval, answer or review deviations | The first stage where supported information is lost or misused |

Wiki retains original graphs, facts and training traces, plus version differences. The proposer can query originals and historical counterexamples by training question, case or stage, and must explain observations, a candidate cause and the expected measurable effect in each patch reason. Held-out question diagnostics do not enter training attribution.

Prefer `scope.evidence_refs` to read `raw` originals by the complete evidence IDs in feedback; linked corrections are included, and training-question or stage filters can further restrict the scope. `raw` and `summary` use no maintenance-model calls. `regroup` reads and merges chunks with model calls, so reserve it for comparing a small, relevant set of originals. `max_chars` limits the reply size only.

`--graph-mode llm`, `--scope sfcp` and Wiki optimization are defaults for new g1 runs. `--graph-rebuild` aliases the LLM mode. Explicit `--graph-mode frozen` or `projection` reproduces historical paths; v0 retains the frozen memory plane. Use a fresh output directory: existing runs and artifacts are not migrated automatically.

Recorded offline tests validate flow, caching, provenance and failure handling. Real model construction quality, causal diagnosis and score improvements require separate controlled acceptance with an operator-specified model.

## Question failure recovery

Candidate feedback exhaustion is recorded as `FeedbackExhausted`, with the original candidates and
check/review feedback. Reviewers receive the preceding revision history as context, never as evidence.
Review exhaustion gets one immediate, fresh question retry per stage identity; transient service faults
retain their backoff. Healthy answers, facts and graphs are reused. Failed checkpoint bytes are archived
before replacement, and the persisted retry budget prevents an automatic retry loop after restart.
Repeated failure remains an execution fault; it is never converted into an abstention. Wiki and the
proposer receive `fault_category` and a specific P.review/P.answer investigation signal.

`execution_status` describes round execution, `adopted_measurement_status` the adopted measurement,
and `unhealthy_stages` retains unresolved historical stage faults. Overall `status` remains failed
while any stage measurement is incomplete. Recovered questions are counted separately in stability
statistics. Decisions retain the score gate and disclose `comparison_status`; an incomplete baseline
cannot establish quality improvement merely because a later version finished its answers.


## Dataset source

Select the raw dataset explicitly with `--dataset-repo justis-xu/memory-eval-zh --dataset-revision main`.
The first run resolves the HF reference to an immutable commit and records the two raw file hashes in
`dataset-source.json`. Continuations reuse that commit. Cache location is optional and does not define
dataset identity. `--data-dir` explicitly opts into local input instead. No implicit local dataset is read.

New g1 runs extract immutable facts from raw messages into `output/memory`, then construct the graph
under the current S. Without a supplied embedding model, this package declares `vector_mode: none`.
Use `--memory-root` to explicitly reuse an existing memory/vector package; frozen/projection modes and
v0 require it. Original QA metrics are the default; an external audited reference requires
`--audited-reference`. Raw datasets and run artifacts are excluded from Git.
