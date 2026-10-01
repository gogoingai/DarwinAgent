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

```
oak/
├── oak/                    # OaK 框架核心（任务无关，两任务共享）
│   ├── config.py           #   模型路由（role→强/快档）/并发/限额/缓存路径
│   ├── llm/                #   LLMClient：流式/重试/磁盘缓存/成本台账（全项目唯一出口）
│   ├── schema/             #   本体 Schema 数据结构 + OWL/HermiT 检查
│   ├── kg/                 #   图构建（键签名折叠/派生边）+ 实体候选契约
│   ├── operators/          #   九件套通用算子 + AST 白名单沙箱
├── datasets/
│   ├── travelplanner/      # 任务一：TravelPlanner（英文基准）
│   │   ├── data/           #   queries 落盘 + 固定抽样索引
│   │   ├── pipeline/       #   复现管线（P1-P6 全流程 + 终态层 + 官方评测适配）
│   │   ├── runs/           #   复现产物（5 轮构建轨迹/推理/缓存/台账）
│   │   └── *.md            #   README / REPRODUCTION / OPTIMIZATION_LOG / ARCHIVE
│   └── locomo/             # 任务二：LoCoMo 中文版（零向量，纯本体）
│       ├── data/           #   locomo10_zh.json（全量中文化数据集）
│       ├── pipeline/       #   复现管线（中文本体/抽取/审计/ReAct/严格判题）
│       └── runs/           #   复现产物（图/答案/报告/失败归因/判分缓存）
├── mem0/                   # 只-ADD 记忆基线核心（中文；mem0 2.1.0 V3 加法管线蒸馏）
├── third_party/            # TravelPlanner 官方仓库 + 环境数据库（不入 git/HF）
└── README.md               # 本文件
```

## 成绩（严格口径，详见各任务目录）

| 任务 | 指标 | 本仓库 | 论文/对照 |
|---|---|---|---|
| TravelPlanner | Final（官方评测器，50 题） | **78%** | 55.9% |
| LoCoMo 中文 conv-26 | 历史 exact（旧修复 gold，199 题） | **79.9%** | 历史原始 gold 口径 73.9%；待统一复评 |
| LoCoMo 中文 conv-26 | 历史混合 gold 宽松评分（已停用） | **89.4%** | 不作跨系统同口径比较 |
| LoCoMo 中文 conv-44 | 严格 exact（旧版栈零调参首跑） | **68.3%** | — |

conv-26 正在固定图上进行原始/审计 gold × 宽松/精准的完整复评，见[独立实验报告](datasets/locomo/runs/experiments/conv26_dual_v4/REPORT.md)。旧评分不代表本轮基线或系统上限，未验证全量十段。

> 历史上限审计与旧评分记录见 `datasets/locomo/pipeline/OPTIMIZATION_LOG.md` 与 `PLAN-90.md`；
> 它们不能证明 90% 可达或不可达。当前结论须依据统一判分与逐要素诊断。

## 快速开始

```bash
uv sync
cp .env.example .env          # 填 ZHIPU_API_KEY（locomo 另可配 LOCOMO_FAST_* 双档）

# TravelPlanner（需先准备官方环境，见 datasets/travelplanner/REPRODUCTION.md）
uv run python -m datasets.travelplanner.pipeline.pipeline.build_loop --rounds 5
uv run python -m datasets.travelplanner.pipeline.pipeline.inference --limit 50

# LoCoMo 中文
uv run python -m datasets.locomo.pipeline.run_anchor            # conv-26 全量迭代
uv run python -m datasets.locomo.pipeline.run_anchor --anchor   # 37 题锚点快速闭环
uv run python -m datasets.locomo.pipeline.run_full              # 全量 10 段

# mem0 只-ADD 基线（自测）
uv run python -c "from mem0.memory_core import Mem0AdditiveCore; print('ok')"
```

## 模型路由

| 档 | 模型 | 用途 |
|---|---|---|
| strong | glm-5.3（智谱） | 本体起草 / 终答 / 判题 / 评判器 |
| fast | deepseek-v4-flash-fast（commandcode 网关，独立 `LOCOMO_FAST_*`） | 抽取 / ReAct 步骤 / 审计 |
| fast 兜底 | glm-5.3-flash（探测自动切换） | 网关不可用时 |

## HF 归档

- TravelPlanner：<https://huggingface.co/datasets/justis-xu/oak-travelplanner>
- LoCoMo 中文：<https://huggingface.co/datasets/justis-xu/oak-locomo>
（含 LLM 请求缓存，可零 API 费用复现全部轨迹；.gitignore 与本仓库刻意不同，见各 ARCHIVE.md）

## 方法论

两线复现的完整经验（TravelPlanner 14 条 + locomo 新增 4 条：别名表是攻击面/
日期不交给 LLM/翻译数据集先测上限/终答上下文相关性排序）：
`datasets/travelplanner/OPTIMIZATION_LOG.md` 与 `datasets/locomo/pipeline/OPTIMIZATION_LOG.md`。
