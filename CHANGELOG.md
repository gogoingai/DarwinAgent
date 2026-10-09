# Changelog

## 0.2.0 — dynamic graphs, explicit datasets and isolated recovery (2026-10-09)

[GitHub release](https://github.com/gogoingai/DarwinAgent/releases/tag/v0.2.0) · [中英文更新说明与迁移提示](docs/releases/0.2.0.md)

- Construct evidence-bound LoCoMo graphs with an LLM under current S and P.extract. Schema or extraction-prompt changes rebuild auxiliary structure while preserving frozen facts and vectors.
- Expose graph structure, unmaterialized declarations, asset change signals and original evidence to Wiki and the proposer for S/F/C/P diagnosis.
- Require an explicit HF repository or local dataset source; pin the resolved commit and file hashes per run. Remove raw data and implicit historical snapshot dependencies from Git.
- Distinguish review exhaustion from tool failures. Give reviewers the preceding revision history and allow one fresh, bounded retry of only settled failed questions; preserve original failures and successful checkpoints.
- Retain recovery records on resume and disclose incomplete score comparisons rather than treating fault recovery as proof of quality improvement.
- Support generic adaptive thinking/reasoning settings and focused Wiki evidence queries.
- Keep README graphics versionless; show GitHub and PyPI release versions with dynamic badges.

**Migration:** New g1 runs default to LLM graphs, Wiki optimization and S/F/C/P scope. Specify `--dataset-repo` or `--data-dir`. Use `--memory-root` explicitly for existing frozen memory; frozen/projection modes and v0 require it. Use a fresh output directory after changing code or construction inputs. No model endpoint is selected automatically.

**Validation:** 590 framework tests (8 resource skips), 18 LoCoMo tests, 11 TravelPlanner tests, Ruff, wheel build and installed-wheel replay/resume passed for the implementation. Release metadata and distribution checks are repeated for 0.2.0. Recorded recovery tests do not establish a model-quality or full-benchmark score gain.

## 0.1.2 — durable loop and Wiki query recovery (2026-10-08)

Published to [PyPI](https://pypi.org/project/darwinagent/0.1.2/) through [Trusted Publishing](https://github.com/gogoingai/DarwinAgent/actions/runs/37717230916). Production pip installation, source hashes, replay and resume were verified outside the checkout; see the [release verification](docs/acceptance/2026-10-08-loop-release.json).

- Preserve candidate smoke-test progress and native model/tool receipts across continuation.
- Restore empty vector snapshots without creating an embedding client.
- Resolve composite training identities consistently and apply Wiki scope filters to the same case/question pair; report empty regroup coverage as partial.
- Bound merged Wiki claims while retaining full originals; recover one JSON object followed only by an orphan Markdown closing fence.
- Pause repeated Wiki queries that make no evidence or regroup progress; resume the same dialogue after an explicit repair/retry.
- Recover saved Wiki maintenance responses and register explicit retries without deleting failed requests.
- Record a real GLM loop with human objective intervention, independent candidate scoring and zero-call completed resume; keep live evidence separate from offline regression results.
- Update bilingual workspace instructions and package documentation while retaining the 0.1.1 onboarding guides.

## 0.1.1 — user onboarding (2026-10-08)

Published to [PyPI](https://pypi.org/project/darwinagent/0.1.1/) through [Trusted Publishing](https://github.com/gogoingai/DarwinAgent/actions/runs/37708266973). Production installation, replay, and resume were verified outside the checkout.

- Put installation and explicit model configuration before framework concepts in both READMEs.
- Add complete CLI and Python live workflows, result inspection, continuation, and troubleshooting.
- Add standalone examples for the bundled loop, user records, and a custom improvement experiment.
- Separate English and Chinese guide bodies and maintainer publishing instructions.
- Validate copyable scripts against the production PyPI package outside the checkout with local simulated endpoints.

## 0.1.0 — experimental release (2026-10-08)

The source version was introduced on 2026-10-07. Its first PyPI release was published on 2026-10-08 through [Trusted Publishing](https://github.com/gogoingai/DarwinAgent/actions/runs/37705060486).

### Engineering governance

- Pin Ruff 0.16.10, editor conventions and an independent CI quality gate; preserve runtime dependency versions.
- Extract Wiki evidence/lessons/context and ordered admission trials/reporting.
- Compose experiment lifecycle, execution stages, optimization, graph supply and round state without changing public APIs.
- Organize behavioral tests and reusable support; retain all 383 original scenarios and seven resource skips.
- Share identical calendar primitives while retaining domain resolvers/scoring; preserve the historical evaluation lock and add the governance lock with six source dependencies.
- Document ownership, source-identity migration and offline acceptance in both languages.

### Package and runtime

- Introduced the independent `darwinagent` distribution/import namespace, Python 3.11+ support, and packaged maintenance task resources.
- Added installed `doctor` and `demo` CLI commands, replay/live modes, bounded HTTP attempts/time, and matching-identity resume.
- Exposed task input/evaluation contracts and common runtime/experiment APIs; separated generic single-endpoint configuration from legacy dataset routes.
- Organized runner feedback, trials, recovery, and statistics helpers while retaining experiment policies.
- Added offline/localhost regressions, explicit external-evidence skips, installed-wheel acceptance, and the CI workflow.
- Added bilingual project guides, Darwin-inspired presentation, editable diagrams, community files, and historical evidence index.
- Fixed provider-error credential redaction and absolute request deadlines, including continuous streaming.

Historical framework versions and dataset findings remain in [the archive index](docs/history/README.md), with their original identity and protocol. Their version numbers do not describe this distribution.
