# Durable progress, human intervention and Wiki queries

The durable Workspace separates immutable content, execution provenance and branch choices. A content ID includes format and original bytes. Canonical JSON is available separately; legacy files and asset source are retained as supplied. `KernelAssets.content_id` excludes generation origin, while historical manifest versions retain their original meaning.

A branch records `working` and `adopted` independently. Human selection changes the working candidate without claiming it scored better. Automatic publication checks the branch revision before changing either choice. Each task can retain several attempts; a failed attempt is never deleted to make a retry look like a first execution.

Real small-task and native improvement-loop checks used the operator-specified `glm-5.3-flash`. See the [2026-10-08 acceptance record](plans/intervention-wiki-loop-acceptance-20261008.md) for measured scope, scores, recovery and pending queries. Offline regressions are reported separately and do not establish full benchmark gains.

## Installed commands

All commands below use the experiment root; persistent records live under its `workspace/` directory. They do not call a model unless `wiki-query --view regroup --allow-model` is explicitly supplied with a configured connection.

```bash
darwinagent workspace --root runs/example status
darwinagent workspace --root runs/example intervene change.json
darwinagent workspace --root runs/example fork alternative --parent main
darwinagent workspace --root runs/example export portable-bundle
darwinagent workspace --root runs/other import portable-bundle
darwinagent workspace --root runs/example preview cases.json --selection selection.json
darwinagent demo --output runs/example --preview --execution selection.json
```

`change.json` is a normal JSON draft, for example `{"kind":"select","candidate":"candidate-version"}` or `{"kind":"code","description":"correct pagination"}`. Code changes are registered for a restart at a safe boundary. Configuration, objective and evaluation drafts are saved as human events; callers apply them at the appropriate execution boundary. An invalid asset draft is retained for correction; registration alone does not admit it for execution.

`cases.json` is a list of objects with `id` and `questions`; each question contains `id`, `text` and optional `parameters`. A selection can contain `mode`, `case_ids`, `question_ids`, `stages`, `branch`, `strict` and `candidate`. Modes are `continue`, `retry_failed`, `rerun`, `fork` and `rerun_all`. Stages include `facts`, `vector`, `graph`, `retrieval`, `answer`, `check`, `review`, `score`, `proposal`, `candidate_check`, `wiki` and `report`.

A preview reports reuse, execution and missing inputs. Missing input is a suggestion to explicitly select the prerequisite; it does not authorize an adjacent stage or a new baseline. The demo accepts `--execution` JSON for actual execution as well as preview. Its flags, LoCoMo and TravelPlanner use the same scope below; `--strict-comparison` opts into frozen comparison checks.

## Requests interrupted across a restart

Requests are saved before submission; complete responses have independent durable receipts. Recovery first reconciles those receipts, then marks submitted requests without receipts `unknown`. It never sends an unknown request again by itself.

```bash
# Run recovery after stopping the previous executor.
darwinagent workspace --root runs/example request recover --executor-stopped
darwinagent workspace --root runs/example request resolve REQUEST_ID --action response --response saved-response.json
darwinagent workspace --root runs/example request resolve REQUEST_ID --action new-attempt
darwinagent workspace --root runs/example request resolve REQUEST_ID --action abandon
```

`new-attempt` creates a prepared request and records possible duplicate cost. It does not dispatch it or overwrite the original unknown request. The transport and task journals must supply the actual response and position; a Workspace receipt does not restore half a streamed response or arbitrary Python process state.

## Safe transfer and old directories

The public export command combines a consistent Workspace snapshot with the entire run directory: step journals, proposal exchanges, Wiki indexes and regroup jobs, graphs, answers and prior rounds. It includes failure evidence, rejected candidates, version registrations and their complete asset dependencies, plus independent request receipts. Asset version identifiers are separate from immutable object IDs. Import materializes each registered bundle coherently and relocates its current path, even when the old directory layout collides. Relative bundle references replace host-specific storage locations. Import creates new current graph and workspace path references while retaining original file bytes and producer identity as evidence. Unequal existing target files are isolated under `imports/` rather than overwritten. The lower-level `export_workspace` function exports only registered Workspace objects and records. Active locks, live stop signals and credentials are excluded; historical control events remain records. The exporter refuses an existing destination directory.

```bash
darwinagent workspace --root runs/example export old-run-bundle --legacy-source /path/to/old-run --path-map path-map.json
darwinagent workspace --root runs/example import old-run-bundle
```

Legacy scanning covers all files and rounds, then follows absolute JSON references recursively. `path-map.json` maps unavailable old absolute paths to current files. Original bytes remain unchanged; legacy provenance is explicitly unknown. Missing external dependencies are reported. Import validates paths and hashes, stages verified objects, and registers only associations with readable dependencies. Broken objects and conflicting branch choices are listed in `not_imported`; existing target successes are preserved. Generic legacy import archives evidence and references. It does not invent a usable task checkpoint from an unfamiliar historical layout.

## Querying and regrouping Wiki evidence

```bash
darwinagent workspace --root runs/example wiki-query 'Check whether q3 pagination advances' --scope scope.json --view raw
darwinagent workspace --root runs/example wiki-query 'Check whether q3 pagination advances' --scope scope.json --view summary
darwinagent workspace --root runs/example wiki-query 'Recheck causes and counterevidence' --scope scope.json --view regroup
```

A scope filters registered case, question, asset or stage identifiers. Replies include references, coverage, missing evidence and a snapshot-bound cursor. Pass `--cursor` to continue the same query snapshot. Start a new query to include newer corrections. Without `--allow-model`, regroup preserves a pending task and returns available raw evidence. With the flag, only the explicitly configured Wiki role is used; no alternate endpoint or model is discovered.

Wiki access permits registered training evidence, assets, corrections and allowed aggregates. Ground truth, raw judge requests and per-question validation/test evidence are outside that interface. Corrections must reference permitted sources. Missing originals remain missing, and partial chunk coverage is not a complete explanation of the requested range.


Use each proposal question's supplied `wiki_scope` to select evidence. Its `training_ids` identify exact case/question pairs; local `question_ids` should be combined with `case_ids` when question names repeat. Qualified training IDs in `question_ids` remain supported for older dialogues. A regroup request with no readable matching evidence returns `partial` with a gap and does not claim that model attribution completed.

Wiki maintenance has a separate recovery entry point:

```bash
darwinagent workspace --root runs/example wiki-maintenance-retry EVENT_ID --reason 'Explicitly retry the interrupted attribution'
```

This only records a new attempt, preserves the old request and possible duplicate cost, and makes no model call. With the existing configured maintainer, `await wiki.resume_maintenance(EVENT_ID)` consumes that attempt; omitting the ID resumes pending attribution tasks. Completed round outboxes do not hide pending attribution. Reliable saved responses are reused; an unknown submission still requires an explicit recovery choice. Daily candidate smoke results and steps persist under `smoke/<asset-version>/`.

## Dataset and demo execution scope

```bash
darwinagent demo --mode replay --rounds 0 --output runs/demo-scoped --execution-stages graph,retrieval,answer,check,review,score
darwinagent demo --mode replay --rounds 0 --output runs/demo-scoped --resume --execution-mode rerun --execution-stages score
python -m datasets.locomo.run --output runs/locomo-small --case conv-26 --preview --execution-question-ids 0,15,23 --execution-stages answer,check
python -m datasets.travelplanner.run --output runs/travel-small --split train --index 0 --preview --execution-stages score
```

Shared options are `--execution-mode continue|retry_failed|rerun|fork|rerun_all`, `--execution-stages`, `--execution-question-ids`, `--execution-branch`, `--strict-comparison` and `--preview`. Daily continuation is the default for ordinary and training execution. The held-out campaign retains its separate frozen protocol; use the training-only path for scoped optimization rather than silently changing validation/test coverage.

LoCoMo score reuse binds actual lock/source/reference hashes; absent audited references do not yield a reliable criterion ID. TravelPlanner includes its actual official rules, database references, query data and evaluator bridge. Custom evaluators without an explicit criterion ID cannot claim reliable score reuse. A changed judge connection alone leaves criterion identity unchanged; an explicit score rerun calls the new judge without generating answers.

A repeated pending/failed Wiki query with unchanged evidence and regroup progress pauses the proposal session. Repair or explicitly retry the Wiki job, then call `session.retry_wiki(reason)` and continue `run(...)`; this rechecks the saved query without discarding the dialogue.
