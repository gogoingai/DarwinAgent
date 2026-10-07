# 架构

[English](../en/architecture.md) · [简体中文](../zh-CN/architecture.md) · [README](../../README.zh-CN.md)

![架构](../assets/architecture-zh-CN.svg)

DarwinAgent 0.1 使用基于图的共同任务运行时。数据集边界只实现 `DatasetAdapter.generation_input(case_id)` 与异步 `Evaluator.evaluate(result)`；前者返回 `CaseInput`，后者返回 `EvaluationResult`。评测参考不进入生成输入。

`Pipeline.run(case, spec, run_config)` 冻结身份并记录产物。`ExtractionAgent` 抽取带来源的事实，确定性装配类型图；`AnswerAgent` 在登记资产和固定协议下调用查询函数、生成回答并审查来源。两个 Agent 共享 `KernelRuntime`。框架负责固定验证与发布，任务检查 C 提供检查意见。

资产由 `TaskSpec` 描述，`KernelBundle` 保存可搬移的指纹版本。S 声明模式，F 在只读算子及受限 AST 下查询，C 检查图或回答快照，P 填充固定角色槽位。[资产边界](#资产边界)明确优化器不能改什么。

`ExperimentRunner` 负责 B0、提案、候选执行、采纳、Wiki 和续跑。`CampaignController` 在其上执行三集合协议。`feedback.py`、`trials.py`、`recovery.py`、`statistics.py` 分别组织训练反馈、试跑、恢复和持久化稳定性统计。它们不改变评测器定义的指标。

## 资产边界

![资产边界](../assets/asset-boundary-zh-CN.svg)

允许提案修改 S/F/C/P；框架执行器、只读权限、固定来源／状态检查、`RunConfig`、评测器和采纳规则位于边界外。C 不能替换固定检查，P 不能引入未登记槽位，F 不能获得任意 Python 权限。

源码入口：[`src/darwinagent`](../../src/darwinagent/__init__.py)。内核 wheel 只包含 `darwinagent` 及演示资源；根目录 `datasets`、`tasks`、`tests`、独立基线和第三方环境不随内核分发。任意外部 Agent 插件尚未实现。
