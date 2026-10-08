# Changelog

## Unreleased — user onboarding

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
