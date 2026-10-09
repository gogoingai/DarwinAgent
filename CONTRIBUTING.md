# Contributing to DarwinAgent

[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md)

DarwinAgent is experimental. Useful contributions include reproducible bug reports, independent task examples, clearer documentation, and controlled evaluation studies. Use [GitHub issues](https://github.com/gogoingai/DarwinAgent/issues) for public discussion; never include credentials or private input data.

## Versioning and compatibility

Versions use `A.B.C`. Except for major architectural changes, routine releases increment only the final component `C` and remain backward compatible, including releases that add features. The published `0.2.0` stays in place; the next routine release is `0.2.1`.

- Fixes, new features, performance improvements, changes to defaults or configuration, and changes to data input methods do not by themselves justify incrementing `A` or `B`. The number of changes does not determine the version increment either.
- Preserve compatibility for public Python APIs, the CLI, configuration, and persisted artifacts, including continuation of existing runs. Supply compatible defaults for new fields, support older formats or provide migration tools, and verify existing usage.
- Before implementing or releasing a major architectural change or an incompatible change, explain its impact, migration plan, and version choice, and obtain explicit confirmation from the project maintainer. Use compatibility layers and a deprecation period where needed.
- Ordinary commits do not automatically create a release. When preparing one, synchronize package metadata, the CLI, the [changelog](CHANGELOG.md), and current documentation. Keep version numbers out of README images and use dynamic version badges.

Automated contributors also follow the root [AGENTS.md](AGENTS.md). See the [PyPI publishing guide](docs/en/pypi-publishing.md) for release steps.

## Set up and verify

```bash
uv sync --frozen --group test
uv run --frozen --group test ruff check .
uv run --frozen --group test ruff format --check .
uv run --frozen --group test python -m unittest discover -s tests -v
uv run --frozen --group test python -m unittest discover -s datasets/locomo/tests -v
uv run --frozen --group test python -m unittest discover -s datasets/travelplanner/tests -v
uv run --frozen --group test python examples/third_domain.py
uv build --wheel
```

Run relevant checks for your change, and report their actual results and skipped dependencies. For installed-package changes, also verify the built wheel outside the checkout using [scripts/check_installed.py](scripts/check_installed.py). Seven full-suite skips currently require archived/official/formal resources; see [acceptance evidence](docs/acceptance/2026-10-07.md).

## Design boundaries

Ruff 0.16.10 is the sole formatter/static checker (Python 3.11 target, 100 columns).
Run `uv run --frozen --group test ruff format .` to format active code. Frozen S/F/C/P
resources, archived scripts, data and independent baselines are excluded because their bytes
are part of evidence identities. Keep `uv.lock` committed and avoid unrelated dependency upgrades.

Place reusable recorded clients, graphs and artifact builders in `tests/support`; these modules
must not define TestCase classes or import scenario files. Tests import their actual production
helper owner. Add scenarios by behavior rather than review date, and preserve original assertions
when moving tests. See the [module map](docs/en/architecture.md) and
[governance report](docs/en/engineering-governance.md).

- Keep the core independent of repository datasets and test modules. Task inputs and evaluator references are separate interfaces.
- Changes to S/F/C/P must respect fixed execution capabilities, checks, and evaluator contracts. Do not relax admission rules to make a candidate pass.
- Preserve historical reports, data, diagnostic scripts, and frozen identities. Add new evidence; do not overwrite scores or recompute old locks.
- Use a fresh run directory when source/model/configuration changes. Live tests require explicit endpoint configuration and request/time bounds; ordinary checks should remain offline.
- Keep English and Chinese current guides aligned. Include editable sources for new diagrams.

## Pull requests

Explain the problem, resulting behavior, and validation. Separate measured findings from hypotheses and limitations. Keep changes focused; identify external dependencies, provenance, and API effects. Preserve third-party copyright and license notices. Contributions are made under the project's MIT license; third-party material remains subject to its own terms.

Review discussions should be respectful, specific, and evidence-led. A public issue is the available project contact; no private reporting address is declared.
