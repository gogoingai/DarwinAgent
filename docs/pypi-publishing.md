# PyPI publishing / PyPI 发布

The package name is `darwinagent`; the first experimental version is `0.1.0`.
Publishing uses GitHub Actions Trusted Publishing, with no permanent API token.
The workflow is manual and only publishes from `main` after checking the package
metadata and running installed-wheel acceptance outside the checkout.

发行名为 `darwinagent`，首个实验版本为 `0.1.0`。通过 GitHub Actions Trusted
Publishing 发布，不需要保存长期 API Token。流程手动触发，只允许从 `main` 发布；
上传前检查包元数据，并在仓库外安装 wheel，验证离线演示与续跑。

## First publication / 首次发布

Sign in to [PyPI account publishing](https://pypi.org/manage/account/publishing/)
and add a pending GitHub publisher with these exact values:

登录 [PyPI 发布设置](https://pypi.org/manage/account/publishing/)，在 GitHub 待发布配置
中填写以下值：

| Field / 字段 | Value / 值 |
| --- | --- |
| PyPI project name / 项目名 | `darwinagent` |
| GitHub owner / 仓库所有者 | `gogoingai` |
| Repository name / 仓库名 | `DarwinAgent` |
| Workflow filename / 工作流文件名 | `publish.yml` |
| Environment name / 环境名 | `pypi` |

This authorizes that specific repository, workflow, and environment to publish
the project. In the repository's GitHub settings, protect the `pypi` environment
with a required reviewer and restrict deployment to `main`.

该配置授权指定仓库、工作流和环境发布这个项目。在 GitHub 仓库设置中，为 `pypi`
环境配置发布审核人，并将可部署分支限制为 `main`。

Run **Publish to PyPI** from the repository's Actions page, choose `main`, and enter
`0.1.0`. Approve the environment deployment after the build job passes.

在仓库 Actions 页面手动运行 **Publish to PyPI**，选择 `main`，版本填写 `0.1.0`。
构建检查通过后，审核并批准环境部署。

A pending publisher **does not reserve the name**. The first successful upload
creates the project. Verify the [PyPI project page](https://pypi.org/project/darwinagent/)
and installation from the production index before reporting publication complete:

待发布配置**不会保留名称**，首次上传成功才会创建项目。发布完成后核对
[PyPI 项目页](https://pypi.org/project/darwinagent/)，并从正式索引安装验证：

```bash
python -m pip install --index-url https://pypi.org/simple darwinagent==0.1.0
darwinagent --version
darwinagent demo --mode replay --rounds 2 --output runs/pypi-replay
```

The first release was published on **2026-10-08 at 07:59 (Asia/Shanghai)** through
[workflow run 37705060486](https://github.com/gogoingai/DarwinAgent/actions/runs/37705060486),
from commit `9b45fddda470c9211b27f7833644de334aceba49`. Both the wheel and source
distribution are available on PyPI. The READMEs, quickstarts, and launch
introductions now use the published installation path.

首个版本于 **2026-10-08 07:59（北京时间）**发布，来源提交为
`9b45fddda470c9211b27f7833644de334aceba49`，wheel 和源码包均已上传。
中英文 README、快速开始和介绍文档已更新为 PyPI 安装方式。

References / 参考：
[PyPI: creating a project through Trusted Publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [PyPA: publishing through GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).
