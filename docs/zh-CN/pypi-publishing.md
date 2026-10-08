# PyPI 发布维护

[English](../en/pypi-publishing.md) · [返回中文首页](../../README.zh-CN.md)

此页面向维护者。用户安装与模型配置见[快速开始](quickstart.md)。

发行名为 `darwinagent`。通过 GitHub Actions 的可信发布授权上传，不需要保存长期发布密钥。工作流手动触发，只允许从 `main` 发布；上传前检查元数据，并在仓库外安装 wheel 包，验证离线演示与续跑。

## 当前发布授权

| 字段 | 值 |
| --- | --- |
| PyPI 项目名 | `darwinagent` |
| GitHub 仓库所有者 | `gogoingai` |
| 仓库名 | `DarwinAgent` |
| 工作流文件名 | `publish.yml` |
| 环境名 | `pypi` |

授权限定到这个仓库、工作流和环境。GitHub 的 `pypi` 环境要求发布审核，并限制部署分支为 `main`。

## 发布新版本

1. 更新版本号、变更记录和发行文档，完成相关检查，将发布提交推到 `main`。
2. 在仓库 Actions 中手动运行 **Publish to PyPI**，选择 `main`，输入与包元数据一致的新版本号。
3. 构建检查通过后，审核并批准 `pypi` 环境部署。
4. 核对 [PyPI 项目页](https://pypi.org/project/darwinagent/)，在干净环境从正式索引安装指定版本，再验证命令行与离线演示。

首次发行版本的安装验证示例：

```bash
python -m pip install --index-url https://pypi.org/simple darwinagent==0.1.0
darwinagent --version
darwinagent demo --mode replay --rounds 2 --output runs/pypi-replay
```

## 首次发布记录

首个版本 `0.1.0` 于 **2026-10-08 07:59（北京时间）**发布，来源提交为 `9b45fddda470c9211b27f7833644de334aceba49`。[发布运行](https://github.com/gogoingai/DarwinAgent/actions/runs/37705060486)上传了 wheel 包与源码包。

待发布授权本身不会保留名称；首次成功上传才创建项目。此项目已经完成首次上传。

参考：[PyPI 可信发布](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)与 [PyPA 的 GitHub Actions 发布指南](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)。

## 0.1.1 发布记录

用户上手文档更新于 **2026-10-08 08:35（北京时间）**发布，来源提交为 `0383a20d7e82da27e466d6dec67091e03d1eeaad`，通过[发布运行 37708266973](https://github.com/gogoingai/DarwinAgent/actions/runs/37708266973)上传。[PyPI 说明页](https://pypi.org/project/darwinagent/0.1.1/)已包含明确的模型配置与 Python 接入链接。

已在仓库外的新环境中从正式索引安装，验证命令行版本 `0.1.1`、SDK 导入、离线回放与续跑。下载文件地址及 SHA256 与 PyPI 元数据一致。发行提交的[自动检查](https://github.com/gogoingai/DarwinAgent/actions/runs/37708232944)已通过，包括 Python 3.11／3.12／3.13 与 macOS。
