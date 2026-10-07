<p align="center"><img src="docs/assets/hero-zh-CN.svg" alt="DarwinAgent — Agent 时代的进化" width="100%"></p>

# DarwinAgent

**面向 Agent 经验驱动递归自改进的开源框架**

[English](README.md) · [简体中文](README.zh-CN.md)

[![Offline framework checks](https://github.com/gogoingai/DarwinAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/gogoingai/DarwinAgent/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.1.0%20experimental-45635c)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-45635c)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-45635c)](LICENSE)

DarwinAgent 的名字来自查尔斯·达尔文与进化论。**Agent 时代的进化**是项目的使命：让 Agent 从经验中适应任务，并保留有效的能力。在这个框架里，资产提案产生候选变化，独立评测和固定准入规则负责筛选，版本化资产与持久化经验 Wiki 负责保留结果，并影响下一轮提案。

DarwinAgent **0.1.0 是实验版本**。它围绕基于图的共享 Agent 运行时，实现了可检查的迭代流程。改进是否发生需要测量，候选也可能被拒绝。当前优化范围是任务资产，框架的执行流程和评测边界保持固定。

## 为什么做 DarwinAgent

一次答对说明这次运行成功，有效的能力还应该在下一次运行中保留下来。DarwinAgent 记录候选改了什么、回答依据哪些来源、评测得到了什么结果、为什么采纳或拒绝，以及哪些经验进入下一轮提案。这样，适应过程就能成为可复查的实验，而不只是一次没有记录的提示词修改。

## 进化循环

![进化循环](docs/assets/evolution-loop-zh-CN.svg)

1. **运行：**通过共同的 `Pipeline` 执行基线，按实际输出评分。
2. **变化：**结合训练证据与 Wiki 反馈，提出有边界的 S/F/C/P 资产修改。
3. **筛选：**验证契约与能力边界，运行候选，按冻结的采纳策略决定是否保留。
4. **积累：**原子发布被采纳的版本；保留拒绝记录，让经验进入下一轮提案。

内核、评测器、权限和采纳规则位于提案边界之外。DarwinAgent 不训练模型权重，也不重写自己的优化器。项目名称表达的是进化论带来的启发，并不表示实现了遗传算法。

## 已实现的能力

| 能力 | v0.1.0 的实际行为 |
| --- | --- |
| 共享任务运行时 | `ExtractionAgent` → 带来源的图 → `AnswerAgent`，由一个 `Pipeline` 执行 |
| 有边界的任务资产 | **S** 本体／模式、**F** 查询工具函数、**C** 任务检查、**P** 角色提示词 |
| 经验积累 | Wiki 保存观察和采纳／拒绝决策，供后续提案使用 |
| 可复查运行 | 资产指纹、冻结运行身份、持久化产物、显式续跑 |
| 独立评测 | 分开的 `DatasetAdapter` 和 `Evaluator`，任务自行定义指标名称 |
| 实验控制 | `ExperimentRunner`；另有 `CampaignController` 管理训练／验证／测试协议 |
| 模型连接 | 默认共用一个显式配置的 OpenAI 兼容 Chat Completions 端点，可按档位覆盖 |
| 使用入口 | 安装后的 CLI、Python SDK、离线回放、真实模型模式、设备维护示例 |

## 跑通循环

**从当前源码检出目录安装**。DarwinAgent 0.1.0 已作为实验性源码版本合入 `main`，尚未发布到 PyPI 或创建 GitHub Release。

```bash
# 在当前源码检出目录运行，需要 Python 3.11+ 和 uv。
uv sync --frozen
uv run darwinagent --version
uv run darwinagent doctor --output runs/doctor
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
```

回放的预期结果：`status=complete`，第一轮采纳，第二轮因 `primary_not_strictly_improved` 拒绝，`http_attempts=0`。独立评测器在一个微型合成任务中，按维护人员／日期两个字段得到 **0.5 → 1.0 → 1.0**。回放使用脚本模型响应、预置 B0 和 **仅 P 资产**的提案，实际执行控制器与运行时。它验证机制，不证明模型学习或基准提升，也没有留出集评测。

输出目录包括：

```text
runs/demo-replay/
├── experiment.json                           # frozen identity
├── B0/evaluation/maintenance-demo.json         # baseline score
├── R1/evaluation/maintenance-demo.json         # candidate score (also R2)
├── R1/optimization/attempt-0/proposal-call.json # proposal input (also R2)
├── optimization/wiki.json                     # durable experience
├── published/current.json                     # accepted asset version
└── demo-summary.json                          # controller summary
```

真实模型运行需要配置端点，并使用另一个输出目录：

```bash
cp .env.example .env
# 在 .env 中填写 DARWINAGENT_API_KEY、DARWINAGENT_BASE_URL、DARWINAGENT_MODEL。
uv run darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

CLI 读取当前目录的 `.env`，不覆盖已有环境变量。真实模式调用实际模型，不回退到录制响应。请求上限按实际发出的 HTTP 尝试计数，包括重试；超时限制覆盖整个运行。真实模型可能在 B0 就全部答对，同分候选应当被拒绝。验收成功表示各阶段执行且决策留痕，不要求分数必然上升。

已有目录只能在模式、模型、配置与源码身份一致时续跑：

```bash
uv run darwinagent demo --mode replay --rounds 2 --output runs/demo-replay --resume
```

也可以从 Python 调用：

```python
import asyncio
from pathlib import Path
from darwinagent.demo import run_demo

asyncio.run(run_demo(Path("runs/python-replay"), mode="replay", rounds=2))
```

[完整快速开始](docs/zh-CN/quickstart.md) · [配置](docs/zh-CN/configuration.md) · [带日期的验收证据](docs/acceptance/2026-10-07.md)

## 接入自己的任务

实现生成输入接口和独立评测器，再通过 `task.yaml` 与 `assets/index.yaml` 登记任务资产。生成输入只携带记录、问题和来源；评测参考保留在评测器内部。

```python
from darwinagent import (
    CaseInput, CorpusBlock, QuestionInput, SourceRef, EvaluationResult,
)

class Records:
    def generation_input(self, case_id):
        return CaseInput(case_id,
            (CorpusBlock(SourceRef("maintenance_record", case_id, "row-1"),
                         "设备 D-17 于 2026-09-01 由林维护。"),),
            (QuestionInput("q1", "谁在什么时候维护了 D-17？",
                           {"serial": "D-17"}),))

class Score:
    async def evaluate(self, result):
        correct = sum(a.status == "answered" and "林" in a.answer
                      and "2026-09-01" in a.answer for a in result.answers)
        faults = sum(a.status == "execution_error" for a in result.answers)
        return EvaluationResult({"correct": correct}, len(result.answers),
                                len(result.answers) - faults, faults, 0)
```

加载实际打包的任务声明与资产登记：

```python
from darwinagent import TaskSpec
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets

assets = load_assets(TASK_ROOT)  # task.yaml + assets/index.yaml + S/F/C/P files
spec = TaskSpec.load(TASK_ROOT / "task.yaml")
```

这两个类接入共同运行时，不替换 Agent 执行流程。[完整离线示例](examples/third_domain.py) 将这两个接口和已登记的 S/F/C/P 资产一起运行：

```bash
uv run python examples/third_domain.py
```

[自定义任务指南](docs/zh-CN/custom-tasks.md)提供完整的真实模型 `Pipeline` 示例及资产契约。任意外部 Agent 的插件接入是后续方向，不是 v0.1 已有能力。

## 架构与文档

![架构](docs/assets/architecture-zh-CN.svg)

| 指南 | English | 简体中文 |
| --- | --- | --- |
| 快速开始 | [Read](docs/en/quickstart.md) | [阅读](docs/zh-CN/quickstart.md) |
| 架构 | [Read](docs/en/architecture.md) | [阅读](docs/zh-CN/architecture.md) |
| 配置 | [Read](docs/en/configuration.md) | [阅读](docs/zh-CN/configuration.md) |
| 自定义任务 | [Read](docs/en/custom-tasks.md) | [阅读](docs/zh-CN/custom-tasks.md) |
| 实验 | [Read](docs/en/experiments.md) | [阅读](docs/zh-CN/experiments.md) |
| 迁移 | [Read](docs/en/migration.md) | [阅读](docs/zh-CN/migration.md) |

[历史证据索引](docs/history/README.md) · [可编辑图源与 PNG](docs/assets/README.md) · [项目介绍稿](docs/launch/introduction.zh-CN.md)

## 状态与后续方向

本地 Python 3.11／3.12／3.13 测试和安装后 wheel 验收记录见[带日期的报告](docs/acceptance/2026-10-07.md)。工作流徽章显示 GitHub CI 的最新状态。历史数据集成绩属于原来的协议和源码版本。

后续计划包括更多独立任务示例、受控留出集实验，以及明确的外部 Agent 接入边界。这些是研究与工程方向，不是已交付能力，也不承诺质量提升。

## 研究来源与社区

S/F 内核受 [*Toward Effective and Reliable LLM Agents via Dynamic Ontology*](https://arxiv.org/abs/2608.22974)（OaK）启发。C/P 资产与经验 Wiki 是本项目的工程扩展。历史复现记录另行索引；论文中的性能数字不作为当前 DarwinAgent 的结果。

[贡献指南](CONTRIBUTING.zh-CN.md) · [English contribution guide](CONTRIBUTING.md) · [变更记录](CHANGELOG.md) · [软件引用](CITATION.cff)

MIT © 2026 DarwinAgent contributors。见 [LICENSE](LICENSE)，第三方材料保留各自的许可证与来源。
