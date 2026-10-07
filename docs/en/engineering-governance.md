# Engineering governance — 2026-10-07

[English](engineering-governance.md) · [简体中文](../zh-CN/engineering-governance.md)

This cleanup preserves DarwinAgent 0.1.0 public APIs, evaluator/adoption rules, execution
capabilities, artifact schemas and resume discipline. It is an offline engineering acceptance,
not evidence of improved model performance. Base: `a6161b7eaaabe0d909ce4cb67b8d4cd821a33d37`.
Earlier [acceptance](../acceptance/2026-10-07.md), reports, original locks and resources remain intact.

## Delivered

- Ruff 0.16.10 is pinned in the test group; `.editorconfig` and CI enforce the same scope and commands. No runtime dependency version was changed in `uv.lock`. The pure-format commits have AST-equivalence evidence (142 active files checked).
- Test support owns reusable data, recorded clients/runners, graphs and artifact builders. All 48 cross-test imports were removed. Large mixed suites are organized by behavior. The [383-scenario old/new mapping](../governance/test-id-map.json) preserves original statuses and assertions; one source-text retry assertion became a stronger 50-attempt runtime check.
- Wiki persistence stays in `WikiMaintainer`; evidence, lessons and context have separate owners. Admission samples, checks, functions, composition and reporting preserve ordered trial checkpoints. Entry files are 325 and 171 lines respectively.
- The controller facade is 411 lines after formatting (1,941 immediately before extraction). Lifecycle, stages, optimization, rounds and graph trials receive explicit dependencies/callbacks. Private iteration state commits after durable decision/publication; hooks and public signatures remain.
- Eleven identical calendar/normalization helpers share one module. Both domains retain their relative resolver and answer-equivalence bodies, including their intentional scoring differences. Adapters/evaluators/run/exports retain domain boundaries; the other active task code received scoped format/import cleanup without policy changes.
- Static guards cover core-to-domain/test/baseline imports and helper-to-runner imports. Support-to-scenario guards also cover literal dynamic imports and import aliases; the core/helper guards currently inspect static imports only. Fresh-process package import is checked against credential reads, model-client construction and directory creation. Wheel contents are checked separately.

## Source and evaluator provenance

The original `datasets/locomo/evaluation_lock.json` is unchanged. The new
`evaluation_lock.governance-20261007.json` binds:

1. `datasets/locomo/evaluator.py`
2. `datasets/locomo/pipeline/protocol.py`
3. `datasets/locomo/pipeline/data.py`
4. `datasets/locomo/pipeline/experiment.py`
5. `datasets/locomo/pipeline/dates.py`
6. `src/darwinagent/operators/calendar.py`

The first four were formatted/import-organized with the governance default introduced separately.
The last two bind the unchanged normalization/scoring dependencies after helper extraction.
New-lock verification passes; the original lock rejects current source; explicit `lock_path`
is preserved. The new lock was created at the first source edit so intermediate checks remained
valid, then expanded after extraction. It does not authorize old checkpoints or rewrite history.
Always use fresh output directories for changed source; see [migration](migration.md).

## Local acceptance

| Check | Actual result |
| --- | --- |
| Python 3.11.15 | 394 core: 387 passed, original 7 skipped; LoCoMo 17 and TravelPlanner 11 passed |
| Python 3.12.13 | Same counts/statuses/skip reasons |
| Python 3.13.13 | Same counts/statuses/skip reasons |
| Ruff check / format | Passed; 223 active Python files formatted |
| Original scenarios | All 383 uniquely mapped and retained; 11 new core boundary/ownership scenarios |
| Admission differential | 37 pairs, 173 ordered report snapshots and 1,313 final rows equal except timings |
| Controller differential | 7 completion/resume/interruption pairs equal after documented timing normalization |
| Protected files | 208 SHA-256 comparisons passed, including original assets, fixtures, data, baselines and non-current docs |
| Wheel | Only framework/resources/metadata; MIT license and demo assets present |
| Installed wheel outside checkout | doctor, two-round replay and resume passed; 0.5 → 1.0 → 1.0; accept/reject; adopted pointer and Wiki dedupe checked |
| Package import | No configuration reads, client creation or output-directory creation |

The three core runs took approximately 27–30 seconds each. Seven skips remain five archived
evidence cases, one external official TravelPlanner environment case and one optional formal
dependency case. Their scenarios were retained; those missing resources were not fabricated.
Comparison fixtures normalize measured time; actual checks execute, but opinion timing is fixed
before timing-dependent Wiki IDs are formed. Both compared implementations use current source
identity, so differential equality does not establish old-lock compatibility.

Reproduce the checks from [CONTRIBUTING](../../CONTRIBUTING.md). Local detailed logs and AST/
differential captures were retained during governance; the CI workflow repeats lint, the three
Python versions, task suites, wheel acceptance and a separate macOS install smoke check.
GitHub results are available on the [Actions page](https://github.com/gogoingai/DarwinAgent/actions).
Only a completed run for the delivered commit establishes its CI result.

No live model request, package publication, GitHub Release or article publication occurred.
