# 接入自己的 Agent

[English](../en/agent-sdk.md) · [迁移说明](migration.md) · [复现方法](agent-runtime-reproduction.md)

本地准备的 0.3.0 尚未发布。新运行时直接接收任务与文件产物，Agent 自己控制执行循环；
DarwinAgent 提供经过准入的工具能力、S/F/C/P 资产、生命周期检查和持久化回执。
原有 `Pipeline` 与图问答命令行继续可用。

## 先运行完整离线示例

在本版本源码目录中使用 Python 3.11 或更新版本：

```sh
uv sync --frozen --group test
uv run --frozen python -m darwinagent.demo.agent_runtime --output runs/agent-offline
# 源码入口等价：
uv run --frozen python examples/agent_runtime.py --output runs/agent-offline
```

也可以先执行 `uv build`，再运行
`python -m pip install dist/darwinagent-0.3.0-py3-none-any.whl`，随后在任意目录调用上面的模块。
示例无需模型地址、密钥或网络模型。[完整源码](../../src/darwinagent/demo/agent_runtime.py)
随安装包交付，可直接阅读和修改。

示例分别运行独立的 `OwnLoopAgent` 与 `ReActAgent`，生成四个真实文件；在 P 保持不变时
修改 F，验证文件内容随之改变；处理可选文件不存在的情况，并验证续跑复用原回执。
`summary.json` 保存各 episode 的事件与用量。ReAct 回复来自离线录制，无模型 HTTP 请求；
缺失的 token 用量不能作为效率提升证据。

## 公开接口

```python
from darwinagent import AgentTask, ExecutionBackend, ExecutionProfile, ToolFailure
from darwinagent.agent import file_tools

class MyAgent:
    def identity(self):
        return {"policy": "report-v1"}

    async def run(self, task, context):
        try:
            source = await context.call("read_file", {"path": "source.txt"})
            text = source["content"]
        except ToolFailure as failure:
            if failure.error_type != "FileNotFoundError":
                raise
            text = task.instruction
        artifact = await context.call("write_file", {"path": "report.txt", "content": text})
        return context.result("报告已保存", artifacts=(artifact,))

async def run():
    task = AgentTask("report", "根据公开任务输入生成报告")
    profile = ExecutionProfile("files-v1", ("read_file", "write_file"))
    backend = ExecutionBackend(
        MyAgent(), file_tools("runs/my-agent/workspace"), profile,
        run_root="runs/my-agent/receipts",
    )
    return await backend.run(task, kernel=None, run_id="report-1")
```

`AgentTask(id, instruction, attachments=(), parameters={})` 冻结公开 JSON 输入。
参考答案和私有评分标准由评测器保管，不放入任务。`EpisodeResult` 包含状态、最终文本、
文件产物、事件与实测用量。状态分别为 `completed`、`rejected`、`failed`、`unknown`、
`budget_exhausted`。

`AgentContext` 提供 `await model(messages, role=...)`、`await call(name, params)`、
`view()`、`check(stage, candidate)`、`prompt(role)` 和 `result(...)`。
Agent 可以自行组织恢复、分支与循环，ReAct 只是框架提供的一种实现。
普通类必须显式实现同步 `identity() -> dict`，声明影响行为的配置与外部版本；
dataclass 默认采用字段，也可以覆盖 identity。临时计数不应混入稳定配置。

真实模型通过 `ExecutionBackend(client=...)` 注入。客户端的异步 `chat(**kwargs)` 返回
`content`、`usage`、`model`；生产 `LLMClient` 使用 `Config`，需显式注册各角色，
例如 `cfg.role_tiers['worker'] = 'strong'`。模型、端点、密钥和 HTTP 限额由宿主确定。

ReAct 显式请求 JSON 模式。已结算回复若不是合法 JSON，会在同一个有界循环中收到格式
反馈；原响应回执保留，每次后续请求仍占原有请求与轮次预算，不执行残缺动作。
此恢复只捕获解析处的 `JSONDecodeError`，以及执行前回复结构校验自己产生的
`ActionProtocolError`；未知提交继续隔离，工具契约、资产 schema、backend 或宿主检查
抛出的错误不作为协议反馈捕获。请求 JSON 模式不保证服务端输出始终合规。

工具回复必须恰好包含非空字符串 `tool` 与对象 `params`。最终回复要求字符串
`final_response`；可选 `artifacts` 必须是 JSON 对象数组，不能是路径字符串数组。
通用产物契约没有统一必填元数据字段。文件产物使用 `[{"path": "工具实际输出路径"}]`，
其他元数据仅保留已完成工具结果支持的值。模型自行纠正不合规条目，运行时不自动把字符串
转换为对象，也不编造路径或哈希。纠正最终回复结构不应重复已完成的写入。

## v4 内核如何改变执行

| 资产 | 执行效果 | 固定边界 |
| --- | --- | --- |
| S | 从任务、附件和实际事件投影有类型的节点与关系，保留来源引用 | 不修改原始事实；不支持的声明直接拒绝 |
| F | 在有步数、容器和时间限制的异步解释器中调用 `call` 与 `view` | 不能执行任意宿主 Python、导入模块或启动进程；宏不能嵌套 |
| C | 检查原语调用前、调用后与最终交付 | 不能删除宿主检查、权限或检查阶段 |
| P | 通过固定角色槽位提供提示词 | 不能更换模型或扩大工具权限 |

使用 `AgentAsset` 与 `AgentKernelAssets(assets, profile)` 构造资产，导出到新目录，
再由 `AgentKernelBundle(path)` 或 `KernelBundle(path)` 格式分发加载。
每个非 S 资产声明对 S 的依赖，每个提示词角色对应一个 P，每个 F 提供符合契约的
`trial_inputs`。`backend.admit(kernel)` 只检查静态接口；实际行为与质量需要执行和独立评测。
完整离线示例包含可运行的 S/F/C/P，并验证仅修改 F 即可改变文件内容。

`ExecutionProfile` 将工具能力、宿主检查、角色与限额固定在可演化资产之外。
C 必须返回布尔值 `True` 才接受。调用后检查拒绝不能撤销已经发生的写入，应保留产物和回执，
同时阻止它作为成功结果交付。

绑定方法形式的宿主检查，其所属对象须通过同步 `identity()` 或 `declaration()` 返回稳定的
JSON 对象，完整声明策略配置；可调用对象遵循同一约定。含闭包、默认参数或全局数据的函数
也须显式声明配置，普通无状态函数可直接使用。持久化时只保存摘要，不写入私有配置明文。
配置声明发生变化后，旧的已完成回放和实验身份失效，应使用新的运行标识。


## 恢复与工作区隔离

每次持久化执行都必须提供明确、安全的 `run_id` 与 `run_root`。
相同身份可复用已完成的响应与结果；任务、源码、工具、profile、模型策略、Agent 配置或
kernel 改变后，需要新的 run ID，不能改写旧身份以绕过校验。

已知的读取失败保存为稳定的 `ToolFailure` 回执，Agent 可以按类型恢复。
即使外部文件后来出现，续跑也保留原来读取失败的事实。
写入或模型请求已提交但缺少权威回执时，状态为 `unknown`，隔离且不自动重发。
保留证据，明确恢复权威回执或决定是否重试；不要删除暂停文件，也不要把丢失响应记为零 token。

包含写工具的实验需要 `backend.for_episode(episode_root, task) -> backend`。
安装包示例的 `WorkspaceBackend` 给出了具体实现：每次 episode 的工具绑定自己的工作区，
回执进入分配的目录，续跑保留已有内容。仅准备当前题的固定材料，保持类、源码、profile、
Agent、传输和工具契约的身份一致，只允许已声明的位置字段变化。共享写工作区会被拒绝；
远程位置是否真正物理隔离仍由宿主负责。

## 实验与费用

将 backend 连同 `AgentExperimentSpec`、`AgentDataset` 与 `EpisodeEvaluator` 传给
`CampaignController` 或 `ExperimentRunner`，直接评测任务与 `EpisodeEvaluation`。
冻结评测器 `declaration()`、源码、模型、数据与预算。训练、验证、测试互斥；准入可以复用
训练题。只有公开训练证据进入提案 Wiki，候选继续使用现有 ProposalSession、Wiki、准入与
版本保留流程。

候选质量不能下降。质量持平时依次比较纠错轮数、展开后的原语调用数、worker token；
未知费用不能视为免费改进。宏内每次 leaf 调用都有独立回执与费用。
`model_calls`、`leaf_calls` 统计持久化预约；`pending_calls`、`unknown_calls`、
`failed_leaf_calls` 解释未完成和失败部分。实际 HTTP 尝试（含重试）读取共享传输总账，
覆盖 worker、judge、proposal、Wiki。报告同时保留两层计数、演化额外费用、失败、未启动项和
缺失用量。离线回放不证明真实模型质量或泛化提升，详见[验收记录](../acceptance/2026-10-09-agent-runtime.md)。
