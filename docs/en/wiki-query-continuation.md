# Layered Wiki queries and five-question offline smoke

Full training source text, questions, generated answers, tool traces and asset code are saved before creating display projections. Immutable objects are also registered in Workspace SQLite with provenance and references. `optimization/evidence` contains the evidence index, query snapshots, replies and resumable regroup jobs. The index uses a process lock. Missing legacy originals are not reconstructed by a model.

`WikiMaintainer.record` saves `_original_training_evidence` separately and removes it from model display payloads. Evidence is indexed by case, question and asset. Context retains structural pagination metadata and late anomaly hints; these hints require contract verification and do not establish a cause. Oversized latest entries retain scores, anomaly pointers and evidence references. `service.correct(target_ref, text, source_refs=...)` adds a refutation rather than replacing history; fresh queries include corrections, while existing cursors keep their original snapshot.

```python
from darwinagent.experiments.wiki_service import WikiQuery

reply = await wiki.service.query(WikiQuery(
    question="Check whether q3 pagination advances; distinguish evidence and hypotheses",
    scope={"case_ids": ["smoke-case"], "question_ids": ["q3"]},
    view="regroup",
    max_chars=8000,
))
data = reply.to_dict()
```

Views are `raw`, `summary`, and `regroup`. Regroup reads original evidence and uses the existing `wiki_maintainer` connection. Without that connection it returns raw evidence and a pending job; it never selects a replacement model.

Replies include status, evidence snapshot/version, facts, hypotheses, support, refutation, uncertainty, original references, coverage, gaps, cursor and job ID. Large payloads use offset-indexed `raw_fragment` or `regroup_fragment` strings; concatenating them produces JSON. If references or coverage themselves exceed the page budget, immutable `reply_fragment` pages encode the entire reply dictionary. Their reconstructed reply may contain a further original-evidence cursor. Every page is checked against its actual JSON serialization size, at most 8,000 characters or the smaller requested budget. Regroup reply cursors never advance a mutable job or trigger further model requests.

Regroup uses original chunks of at most 9,000 characters, checks complete model input against 24,000 characters, and requires cited findings within 4,000 characters. Completed chunks and binary merge groups are durable. Merging preserves support, refutation and uncertainty, including cross-chunk contradictions and the distinction between association and causality. A remaining merge/input problem is explicitly reported as pending instead of claiming full coverage.

Successful responses are reused. Unknown submissions and saved failures are not implicitly resent. After an explicit recovery decision, `service.retry_job(job_id, chunks=[1])` creates a new attempt only for unfinished selected chunks. `retry_merge=True` permits a new failed merge attempt while completed groups remain reusable. SQLite also records the regroup attempt's progress and final result.

Allowed source classes are training, asset, correction and allowlisted aggregate statistics. Unknown or restricted source references are rejected, including links from human annotations. Aggregate wrappers cannot carry per-question answers or diagnostics. Nested gold, expected/standard answer and judge request fields receive additional rejection checks. Callers must accurately classify provenance: the service cannot infer a deliberately false source declaration from arbitrary prose. Answer agents do not receive the optimization Wiki.

Run the prepared-graph offline smoke with:

```sh
PYTHONPATH=src python scripts/smoke_intervention.py --mode replay --output /tmp/darwin-five-smoke
```

The public Pipeline handles facts, dates, 130-row pagination, multiple tool results and valid abstention. The smoke changes concurrency and checks that successful answers cause no new generation calls. A single proposal session requests original Wiki regrouping and then continues with `no_change`. Its independent fixture evaluator remains outside proposal/Wiki inputs. Sources, graphs, registered assets, responses, traces, queries, regroup/merge jobs and the report remain in the output directory.

This is offline mechanism verification, with zero HTTP attempts. It does not establish live model quality or score improvement. The live entry requires an explicit user-selected connection. It requires an explicit user-provided `--model` plus `DARWINAGENT_BASE_URL` and `DARWINAGENT_API_KEY`; all roles use that exact model. It neither discovers alternatives nor calls an embedding endpoint. Run `--mode live --model '<user-specified model>' --output <separate-live-directory>` only after the model is provided. Live/replay directories cannot be mixed. Shared limits enforce concurrency 1, 80 actual HTTP attempts including retries, and 900 active seconds. The first proposal action must regroup original q3 evidence and then continue the same session.

`RunResult.answer_provenance` keeps each reused answer's original case, graph, asset and producer rather than assigning the new execution view as its source. Missing legacy sources are marked unknown; Wiki never joins an old answer with changed current corpus text. Regroup and merge requests now use the Workspace request ledger, preserving distinct awaiting-budget, unknown and abandoned controls. In raw replies, `matched` describes the query's found set, while `covered` describes only fragments actually returned on this page.

This delivery ran the five-question generation and three additional cases (six questions) with the user-selected `glm-5.3-flash`. See the [acceptance record](../plans/intervention-wiki-progress.md) for final dynamic regroup status, actual failures and independent checks. Offline replay cannot establish live acceptance, full-dataset accuracy or optimization gains.

Set `RunConfig(wiki_max_tokens=12000)` to provide a separate Wiki completion budget when reasoning consumes the answer budget. Chunk findings use `{"text":"fact","ref":"original reference"}`; merged findings use `{"text":"finding","evidence_refs":["original reference"]}`. Repair feedback names the exact fields and permitted references; unsupported references remain rejected.

Invalid proposal cursors become persisted failed replies in the same dialogue. Keep question/scope/view unchanged for continuation, or use null for a fresh query. Replies include a canonical `continuation_query`. The live script accepts `--active-seconds`, `--max-requests`, `--wiki-max-tokens` and explicit `--retry-failed-wiki`. Increased limits preserve prior consumption; known-failure retries preserve old receipts and do not retry unknown submissions. `scripts/smoke_extended.py` adds subject/date, historical attribution, missing evidence, 75-row pagination and interruption after durable receipt storage.

Completed cross-chunk conclusions appear before chunk details in large regroup replies. `--refresh-wiki-reply` explicitly updates the same proposal dialogue from saved completed findings without repeating Wiki model calls. Old reply cursors retain their original order and snapshot.
