---
license: mit
language: zh
tags:
- long-conversation-memory
- ontology
- locomo
- benchmark-reproduction
---

# oak：动态本体（OaK）复现仓库

> OaK（arXiv:2608.22974，*Toward Effective and Reliable LLM Agents via Dynamic Ontology*）
> 的复现与扩展：**一个框架，两个数据集基准**，外加一个只-ADD 的中文记忆基线。
> 数据集、记忆、本体全中文（locomo 轨道）。

## 目录结构

当前固定框架为 **0.4.0**（抽取/构图两阶段 + 事实锚定图 + 三集合 campaign）：数据集实现输入适配和独立评测，生成与迭代由 `oak` 统一执行，只优化有能力边界的 S/F/C/P 资产。活动 H、`oak_domains` 与私有生成流程已移除，发行 wheel 只包含 `oak`。

```text
oak/                         # 固定框架
├── contracts.py             # DatasetAdapter / Evaluator 与标准输入输出
├── config.py                # 冻结 RunConfig
├── engine/pipeline.py       # 共同 Pipeline：身份、恢复、产物
├── agents/                  # ExtractionAgent / AnswerAgent / 固定协议
├── kernel/                  # 登记、Runtime、F/C、固定验证、反例、原子版本
├── experiments/             # 通用冷启动、提案、两轮控制器、采纳策略
├── schema/                  # 本体解析、静态与图实例公理验证
├── kg/                      # 类型图和来源
├── operators/               # 只读算子 + 受限 AST 解释器
├── llm/                     # 模型通信和连接装配
└── runtime/                 # 身份、预算、原子持久化

tasks/                       # 声明及资产，不实现执行流程
├── conversation_memory/     # 仅 task.yaml，无历史 LoCoMo 种子
├── travel_planning/         # task.yaml + assets/{S,F,C,P}
└── device_maintenance/      # 第三任务声明和资产

datasets/
├── locomo/                  # adapter.py / evaluator.py / exports.py / run.py
└── travelplanner/           # 同样两个接入类；共用 Pipeline
    # 两者保留 data/、runs/、冻结评测及原路径兼容代码

examples/third_domain.py      # 两个接口 + 任务资产，在仓库外跑共同 Pipeline
tests/{unit,integration,portability}/
docs/                        # 架构、类图、资产能力边界和使用入口
dist/                        # 新 wheel 仅包含 oak；旧发行物保留
mem0/                        # 独立的只-ADD 中文记忆基线
third_party/                 # 已允许的 TravelPlanner 环境数据
```

[架构、类图与能力边界](docs/ARCHITECTURE.md) · [接入与运行](docs/PORTABLE-USAGE.md) · [实施和验收](docs/PORTABILITY-PLAN.md)。框架实现验收与模型成绩分开记录；冷启动结果以对应运行目录产物为准，不能用离线录制响应代替真实实验。

0.4.0 通过 119 项离线检查（核心/接入/控制器/三集合 campaign 98 + 冻结评测与旅行资产 21）和仓库外 wheel 验收；抽取按消息边界分批（≤2000 字符）+ 截断二分 + 定位化错误反馈，图由原子记忆确定性装配并做 round-trip 校验。0.3.2 的真实冷启动失败记录与归因见[修复与实验记录](docs/FRAMEWORK-REPAIR-LOG.md)。下表为历史任务成绩。

## 成绩（严格口径，详见各任务目录）

| 任务 | 指标 | 本仓库 | 论文/对照 |
|---|---|---|---|
| TravelPlanner | Final（官方评测器，50 题） | **78%** | 55.9% |
| LoCoMo 中文 conv-26 | 历史 exact（旧修复 gold，199 题） | **79.9%** | 历史原始 gold 口径 73.9%；待统一复评 |
| LoCoMo 中文 conv-26 | 历史混合 gold 宽松评分（已停用） | **89.4%** | 不作跨系统同口径比较 |
| LoCoMo 中文 conv-44 | 严格 exact（旧版栈零调参首跑） | **68.3%** | — |

conv-26 既有固定图的原始/审计 gold × 宽松/精准复评见[独立实验报告](datasets/locomo/runs/experiments/conv26_dual_v4/REPORT.md)。旧评分不代表本轮基线或系统上限，未验证全量十段。

> 历史上限审计与旧评分记录见 `datasets/locomo/pipeline/OPTIMIZATION_LOG.md` 与 `PLAN-90.md`；
> 它们不能证明 90% 可达或不可达。当前结论须依据统一判分与逐要素诊断。

## 快速开始

```bash
uv sync
cp .env.example .env          # 填 ZHIPU_API_KEY（locomo 另可配 LOCOMO_FAST_* 双档）

# TravelPlanner（需先准备原官方环境和评测依赖）
uv sync --extra benchmarks
uv run python -m datasets.travelplanner.run --split train --index 0 --output datasets/travelplanner/runs/my_framework_case

# LoCoMo 中文（三集合：train=conv-26 / validation=conv-47 / test=conv-49）
uv run python -m datasets.locomo.scripts.precheck --output datasets/locomo/runs/atomic_v1   # 真实模型预检（campaign 的启动门）
uv run python -m datasets.locomo.run --campaign --output datasets/locomo/runs/atomic_v1     # B0 -> Rn 无限迭代（--rounds 可设上限）
uv run python -m datasets.locomo.run --campaign --output datasets/locomo/runs/atomic_v1 --stop  # 叫停：当前轮完成后锁定候选并进入验证/测试

# 框架、接入与控制器检查，及显式录制响应的第三任务示例
uv run python -m unittest discover -s tests
uv run python examples/third_domain.py

# mem0 只-ADD 基线（自测）
uv run python -c "from mem0.memory_core import Mem0AdditiveCore; print('ok')"
```

## 模型路由

| 档 | 模型 | 用途 |
|---|---|---|
| strong | glm-5.3（智谱） | 资产初始化 / 提案 / 终答 / 语义审查 / LoCoMo 独立评分 |
| fast | deepseek-v4-flash-fast（commandcode 网关，独立 `LOCOMO_FAST_*`） | 抽取 / 工具选择 |

真实实验提前冻结模型路由；变更路由需重新冻结工程基线并使用新目录。

## HF 归档

- TravelPlanner：<https://huggingface.co/datasets/justis-xu/oak-travelplanner>
- LoCoMo 中文：<https://huggingface.co/datasets/justis-xu/oak-locomo>
（含 LLM 请求缓存，可零 API 费用复现全部轨迹；.gitignore 与本仓库刻意不同，见各 ARCHIVE.md）

## 方法论

两线复现的完整经验（TravelPlanner 14 条 + locomo 新增 4 条：别名表是攻击面/
日期不交给 LLM/翻译数据集先测上限/终答上下文相关性排序）：
`datasets/travelplanner/OPTIMIZATION_LOG.md` 与 `datasets/locomo/pipeline/OPTIMIZATION_LOG.md`。
