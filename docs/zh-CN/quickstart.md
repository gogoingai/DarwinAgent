# 快速开始

[English](../en/quickstart.md) · [简体中文](../zh-CN/quickstart.md) · [README](../../README.zh-CN.md)

从当前源码检出目录运行，需要 Python 3.11+ 和 uv。此审查分支未发布到 PyPI；不要把远端主分支当作已验证的 0.1.0 安装来源。

```bash
uv sync --frozen
uv run darwinagent --version
uv run darwinagent doctor --output runs/doctor
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay --resume
```

`doctor` 默认只检查安装、资源、输出可写性及真实模式配置是否齐全，不发请求。`--check-model` 会额外发起一次真实模型检查，最长 30 秒。`--version` 输出 `darwinagent 0.1.0`。

回放执行真实 `Pipeline`、`ExperimentRunner`、Wiki、准入与发布。它使用脚本响应、预置 B0、只修改 P 提示词。独立评测器按一个合成问题的维护人员与日期两个字段评分，结果为 0.5 → 1.0 → 1.0；R1 采纳，R2 同分拒绝，HTTP 请求为 0。没有留出集，也不能据此得出模型能力提升结论。

真实模式使用另一个输出目录：

```bash
cp .env.example .env
# Edit .env before running live mode.
uv run darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

`.env` 填写 `DARWINAGENT_API_KEY`、`DARWINAGENT_BASE_URL`（提供商的 API 根地址）和 `DARWINAGENT_MODEL`。CLI 读取当前目录 `.env`，已有环境变量优先。真实模式不使用脚本回退。B0 可能已全部答对，因此两轮都被拒绝也可以是正确的执行结果。每个实际 HTTP 尝试（包括重试）计入请求上限；超时覆盖整个运行。CLI 轮数范围为 0–10。

已有实验目录需要 `--resume`，且模式、模型、配置、源码身份必须匹配。改变其中任何项请使用新目录；不要覆盖已冻结实验。

Python 入口：

```python
import asyncio
from pathlib import Path
from darwinagent.demo import run_demo

asyncio.run(run_demo(Path("runs/sdk-replay"), mode="replay", rounds=2))
```

检查产物：`experiment.json` 为冻结身份；`B0/evaluation/`、`R1/evaluation/`、`R2/evaluation/` 为独立评分；`optimization/wiki.json` 为持久经验；`R*/optimization/attempt-0/proposal-call.json` 为提案输入；`published/current.json` 指向采纳版本；`demo-summary.json` 汇总运行。真实模式另有 `http_attempts.json`。

继续阅读[配置](configuration.md)、[实验协议](experiments.md)和[验收记录](../acceptance/2026-10-07.md)。

## 使用 pip 安装

在同一个源码检出目录，也可以使用标准 Python 工具：

```bash
python3 -m venv .venv-pip
# Windows: .venv-pip\Scripts\activate
source .venv-pip/bin/activate
python -m pip install .
darwinagent doctor --output runs/pip-doctor
darwinagent demo --mode replay --rounds 2 --output runs/pip-replay
```

这里安装的是当前源码，不依赖已发布的 PyPI 包。
