# PyPI publishing

[简体中文](../zh-CN/pypi-publishing.md) · [English home](../../README.md)

This guide is for maintainers. Package users should start with the [quickstart](quickstart.md).

The distribution name is `darwinagent`. Publishing uses GitHub Actions Trusted Publishing without a permanent API token. The workflow is manual and publishes only from `main`, after metadata checks and installed-wheel replay/resume acceptance outside the checkout.

## Publisher identity

| Field | Value |
| --- | --- |
| PyPI project | `darwinagent` |
| GitHub owner | `gogoingai` |
| Repository | `DarwinAgent` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

Authorization is scoped to this repository, workflow, and environment. The GitHub `pypi` environment requires deployment review and restricts deployment to `main`.

## Publish a new version

1. Update version declarations, the changelog, and release documentation. Run the relevant checks and push the release commit to `main`.
2. Run **Publish to PyPI** manually in Actions, choose `main`, and enter the new version matching package metadata.
3. Approve the `pypi` environment deployment after the build passes.
4. Verify the [PyPI project page](https://pypi.org/project/darwinagent/), install that version from the production index in a clean environment, and check the CLI and replay.

For example, the first release can be verified with:

```bash
python -m pip install --index-url https://pypi.org/simple darwinagent==0.1.0
darwinagent --version
darwinagent demo --mode replay --rounds 2 --output runs/pypi-replay
```

## First release record

Version `0.1.0` was published on **2026-10-08 at 07:59 (Asia/Shanghai)** from commit `9b45fddda470c9211b27f7833644de334aceba49`. [Workflow run 37705060486](https://github.com/gogoingai/DarwinAgent/actions/runs/37705060486) uploaded both the wheel and source distribution.

A pending publisher alone does not reserve a name; the first successful upload creates the project. This project has completed that upload.

References: [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) and [PyPA's GitHub Actions publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).
