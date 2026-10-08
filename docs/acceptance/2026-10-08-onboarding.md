# User onboarding acceptance — 2026-10-08

[简体中文](2026-10-08-onboarding.zh-CN.md)

Scope: current user guides and three standalone Python examples, using the published `darwinagent==0.1.0` package on macOS/Python 3.13. The subprocesses ran outside the checkout and imported from the installed package in `site-packages`.

The installed wheel came from the production PyPI index, with SHA256 `78982a7bed7b0bda6443fbcc3e811f9f7417610e5c6b4f2aa49bfa801b2e7542`.

## Executed checks

All 14 scenarios passed:

| Scenarios | Result |
| --- | --- |
| Installed CLI version, offline doctor, replay, and resume | Complete; replay attempts 0; first round accepted and second rejected |
| Current-directory `.env`, offline readiness, connection probe, CLI live loop, and resume | Offline check dispatched 0 requests; probe 1; demo 18; completed resume dispatched no new requests |
| Full Python scripts copied from English and Chinese quickstarts | Both completed with 18 local HTTP attempts |
| Full scripts copied from both custom task guides | Both answered; incomplete answers scored 0 |
| Both custom task scripts with complete local responses | Both scored 1, with no generation faults |
| Full scripts copied from both custom experiment guides | Both completed two rounds with 20 attempts, accepted then rejected |
| All three standalone example files | Completed; attempts 18, 5, and 20 respectively |
| Copy the installed task into a fresh user project | Declaration, registry, schema, query, check, and all four prompts were present |

All model requests in these checks went to a local simulated streaming Chat Completions endpoint. It supplied scripted responses and retained request paths and model names. No external model provider was called. These checks verify integration and example execution; they do not measure model quality or benchmark gains.

Additional checks passed: syntax parsing of 19 Python blocks in current language guides, local Markdown file/heading links, repository Ruff checks, package build, strict Twine metadata checks, and whitespace validation. Context-dependent snippets were syntax-checked in their intended context rather than claimed as standalone programs.

The guides now separate installation, model configuration, live execution, Python integration, offline replay, and result inspection. Maintainer publishing instructions have separate language editions. Historical acceptance reports retain their original dates and scope.

## 0.1.1 publication

The user onboarding update was published on **2026-10-08 at 08:35 (Asia/Shanghai)** from commit `0383a20d7e82da27e466d6dec67091e03d1eeaad` through [workflow run 37708266973](https://github.com/gogoingai/DarwinAgent/actions/runs/37708266973). The [PyPI page](https://pypi.org/project/darwinagent/0.1.1/) now includes explicit model configuration and Python integration links.

Installation from the production index was verified in a fresh environment outside the checkout: CLI version `0.1.1`, SDK imports, offline replay, and resume passed. The downloaded wheel URL and SHA256 matched PyPI metadata. The release's [CI](https://github.com/gogoingai/DarwinAgent/actions/runs/37708232944) passed on Python 3.11/3.12/3.13 and macOS.
