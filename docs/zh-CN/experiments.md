# 实验与证据

[English](../en/experiments.md) · [简体中文](../zh-CN/experiments.md) · [README](../../README.zh-CN.md)

![进化循环](../assets/evolution-loop-zh-CN.svg)

`ExperimentRunner` 冻结运行身份并执行 B0 → 提案 → 准入 → 候选生成／评测 → 采纳 → Wiki。独立评测器返回指标；`AdoptionPolicy(primary, non_decreasing)` 要求主指标严格提升、指定指标不下降，并检查评测完整性与故障。外部传输故障与确定性生成故障分别披露；评测故障仍会阻止采纳。以实际决策中的 reasons 为准。

演示调用 `run_demo(..., rounds=2)`，使用预置 B0，仅允许 P 修改，没有验证集或测试集。它不能替代正式留出实验。`ExperimentRunner` 可以配置逐轮验证门；这与三集合 campaign 是不同的协议。

`CampaignController` 使用 `ExperimentSpec` 中互不相交的 train、validation、test case id，训练结束后锁定候选，再统一验证／选择版本，最后执行一次测试。`SelectionPolicy` 由独立指标名定义。该协议已实现，但本轮演示及真实烟雾验收没有执行完整三集合研究。

```python
from darwinagent import ExperimentSpec, AdoptionPolicy, SelectionPolicy

spec = ExperimentSpec(
    train=("train-case",), validation=("validation-case",), test=("test-case",),
    adoption=AdoptionPolicy("correct", ()),
    selection=SelectionPolicy("correct", "correct"), rounds=2,
)
```

这个片段创建协议声明，不发模型请求，也不包含数据集实现。多 case 汇总采用冻结的 sum 规则；评测器应返回可相加的指标计数。演示中的单 case accuracy 不能直接作为多 case 平均准确率使用。

## 记录什么

保留资产版本、源码／模型身份、每个 case 的来源与答案、评分、提案输入、准入／拒绝原因、Wiki 和请求计数。续跑只能使用匹配的冻结身份；不要重写旧锁让新源码通过。数据集实验需要额外的数据与评测器依赖，见[历史索引](../history/README.md)，不随核心 wheel 分发。

[2026-10-07 验收报告](../acceptance/2026-10-07.md)将本地回归、离线回放和真实模型烟雾验收分开记录。请求数量与耗时不等同于 token 用量或金额。
