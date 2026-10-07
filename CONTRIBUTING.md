# Contributing to DarwinAgent

[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md)

DarwinAgent 0.1.0 is experimental. Useful contributions include reproducible bug reports, independent task examples, clearer documentation, and controlled evaluation studies. Use [GitHub issues](https://github.com/gogoingai/DarwinAgent/issues) for public discussion; never include credentials or private input data.

## Set up and verify

```bash
uv sync --frozen --group test
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s datasets/locomo/tests -v
uv run python -m unittest discover -s datasets/travelplanner/tests -v
uv run python examples/third_domain.py
uv build --wheel
```

Run relevant checks for your change, and report their actual results and skipped dependencies. For installed-package changes, also verify the built wheel outside the checkout using [scripts/check_installed.py](scripts/check_installed.py). Seven full-suite skips currently require archived/official/formal resources; see [acceptance evidence](docs/acceptance/2026-10-07.md).

## Design boundaries

- Keep the core independent of repository datasets and test modules. Task inputs and evaluator references are separate interfaces.
- Changes to S/F/C/P must respect fixed execution capabilities, checks, and evaluator contracts. Do not relax admission rules to make a candidate pass.
- Preserve historical reports, data, diagnostic scripts, and frozen identities. Add new evidence; do not overwrite scores or recompute old locks.
- Use a fresh run directory when source/model/configuration changes. Live tests require explicit endpoint configuration and request/time bounds; ordinary checks should remain offline.
- Keep English and Chinese current guides aligned. Include editable sources for new diagrams.

## Pull requests

Explain the problem, resulting behavior, and validation. Separate measured findings from hypotheses and limitations. Keep changes focused; identify external dependencies, provenance, and API effects. Preserve third-party copyright and license notices. Contributions are made under the project's MIT license; third-party material remains subject to its own terms.

Review discussions should be respectful, specific, and evidence-led. A public issue is the available project contact; no private reporting address is declared.
