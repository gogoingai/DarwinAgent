# DarwinAgent LIFT 集成 / LIFT integration

本目录是可选 Python 3.12+ 基准集成，不进入核心 wheel。当前路线通过**本地执行适配**
复用固定上游 `execute_task`、`run_task` 和原生内容评判提示、解析与计分；不是完整容器
生命周期复现。注册 wrapper 不修改上游源码。

This optional integration stays outside the core wheel. The current route is a **LOCAL adaptation**
of pinned upstream task execution and native content judging. It is not complete container
reproduction, and the registration wrapper leaves upstream source untouched.

冻结源码 `97c6cbb`，0.3.0 仅本地准备、未发布。q5 与独立 q6 三组实测已完成，
当前wheel/源码外安装与sdist打包检查通过；最终文档审查和需求审计待补。详细实测与历史记录见
[验收报告](../../docs/acceptance/2026-10-09-agent-runtime.md)，复现见
[中文](../../docs/zh-CN/agent-runtime-reproduction.md) / [English](../../docs/en/agent-runtime-reproduction.md)。

## 固定输入与对照 / Pinned inputs and comparisons

- LIFT：`9d282e415c5e3b0f65b82c8758a013b269583d93`。
- EALE：`419789ed1662e7b9ad16aa6620a4b31449612948`；清单逐文件校验。
- q1：训练/行为准入；q2：独立候选选择；选择冻结后的 q5：开发诊断。
- 开发清单只含 q1/q2/q5；q6 独立材料只在开发结束且门检查通过后由协调者准备。
- A：同一 ReAct 和固定原语、无 kernel；B：K 0；C：选中 K 1，拒绝则显式 K 0 回退。
- A/B/C 的历史和工作区独立，权限、模型、生成与原生交互契约一致；C=K 0 也不复用 B。

## 当前 MiniMax 本地路线 / Current MiniMax local route

使用现有 Python 3.12 环境、固定上游与已有材料；不启动 Docker、Langfuse、后台服务、
子工具进程，也不为本轮安装依赖。复制 [minimax.local.example.json](minimax.local.example.json)
并填入独立私有 `run_root`、`data_root`、`budget_root`。旧 [pilot.example.json](pilot.example.json)
保持默认 HTTP 100/worker 8；不要改写它以默认启用无限调用。

本轮显式模型为 `MiniMax-M3.1-Flash-Preview`、端点 `https://api.minimax.cn/v1`、温度 0，
`provider_request_policy:minimax-adaptive-v1`。所有角色使用 `thinking.type=adaptive`、
`reasoning_effort=max`、生成上限 16384；本地 `max_tokens:16384` 配置在请求时转换为
`max_completion_tokens:16384`，不加旧推理 buffer。没有模型/端点回退。
模型/端点环境变量必须与配置完全相同；凭据只由宿主 `DARWINAGENT_API_KEY` 提供。
以下会产生真实模型请求，只有已授权协调者执行：

```sh
DARWINAGENT_MODEL=MiniMax-M3.1-Flash-Preview \
DARWINAGENT_BASE_URL=https://api.minimax.cn/v1 \
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.local \
  --upstream /path/to/pinned/LIFT --config /private/minimax-local.json \
  --run-id UNIQUE_MINIMAX_DEVELOPMENT_RUN
```

显式 `unbounded_model_calls:true`、`worker_limit:null`、`http_cap:null` 使 worker/session、
worker/turn、judge、proposal、Wiki 的模型调用上限为真实 None/null。次数仍逐次持久保存，
不是大整数占位，也不重置计数。每 Episode 64 个原语 leaf、3 次原生 worker↔judge 交互
（最多 2 次纠错）、一个候选和一轮进化保持固定。默认 API 的 model 8/ReAct 16/generation 4096
保持不变；取消调用数量上限不取消请求时限、宿主检查、未知写隔离或恢复闸门。

`budget_root` 与比较目录独立。MiniMax 使用新的明确策略身份，历史 GLM 账本与未知暂停
保留；之后同一 MiniMax 身份内的角色、进程和新比较目录共用持续计数的总账。
源码、模型、生成、权限或选中资产改变时，必须新建比较身份，保留原始失败和收据。
未知请求/写入禁止自动重放；历史恢复 API 不得用于放行这类未知状态。

All roles use the exact MiniMax model/endpoint, adaptive/max and generation 16384. Explicit null
model-call caps keep durable counters and unknown-effect quarantine. Tool rights,64 leaves per
episode,3 native interactions and one candidate/round remain fixed. Existing API defaults and
legacy example caps remain unchanged. The new MiniMax identity does not resolve old GLM pauses.

## 工具、提案与独立性 / Tools, proposal and isolation

本地提供五项固定原语：`list_files`、`read_text`、`read_table`（CSV/XLSX 首表）、
`transform_tables`（内存 SQLite 只读 SELECT）、`write_text`。路径、符号链接和写入限制在
独立题目工作区；judge 只注册读工具。私有条件、凭据、账本、kernel 和选中资产不在工作区内。
不提供任意宿主代码执行；启动时禁用 dotenv 发现和原生 pre-chat 上报。

原生内容 judge 保留上游提示、条件、解析和计分。仅澄清结果 JSON 必须放在外层
`final_response` 字符串中，不接受裸结果自动终结，不改原生成功标准。
ReAct 可使用单动作、final 或 1–16 项 `actions` 批量；整批契约先验证，再顺序经过
`context.call`，每个原语仍检查/计账。已知工具或 C 失败停止剩余动作，未知效果隔离。
ReAct 每次刷新一份最新 view，按顺序保留观察、检查、错误、反馈与不可变回执。

K 0 的 `inspect_materials` 从公开 S Material 路径读取最多 16 个 TXT/MD/CSV/XLSX；
不读取私有评判。完整 S/F/C/P 提案空间向提案模型开放，仍受固定 ExecutionProfile、工具
契约与准入检查约束，不用提示优化绕过现有 ProposalSession/Wiki/选择流程。
本次真实候选仅修改 worker P，不能声称已经实测 S/F/C 变更。

q1 相同任务/K 0/身份收据可在 warmup/准入/训练间复用；候选 q1 同样复用公开证据。
q2 原始私有评判不进训练 Wiki。原件通过精确 `wiki_scope` 引用；证据 SHA 不冒充 training ID。
提案/Wiki 无限调用仍保存全部查询/费用；重复证据提示、复制续页纠正不丢弃历史。
原始文本、来源覆盖及旧存储游标兼容；小回复预算也必须可完整遍历，不以首条返回证明完成。
训练失败跳过提案，独立诊断 q2；进化不可用显式回退 K 0；已知失败可以继续独立任务。

## 独立 q6 确认 / Independent q6 confirmation

开发 q5 A/B/C 调度终态后，协调者以单独 q6 查询/材料清单及 SHA 256 准备 confirmation 数据。
配置设置 `mode:confirmation`、`selection_run_root` 指向已完成开发根、新 `run_root`、q6
专属 `data_root`，沿用同一 MiniMax 总账、源码、请求策略、生成、有效 cap、K 0 和选中资产。
模式检查开发调度终态和冻结身份，独立执行 q6 A/B/C，不训练、不调用 proposal/Wiki、不进化。
私有要求只给 judge；q6 不加入训练。独立确认可得负面结果，不以完成执行代表质量提升。
本轮冻结确认已完成，安全汇总显示 A/B/C 最终内容 0.79/0.86/0.93，三组健康且
native success 均 false；worker 调用 15/18/20、leaf 24/28/59，均为 3 次交互/2 次纠错。
C 的 worker 调用、leaf、token 更多；协调者误读旧GLM暂停范围后暂停了MiniMax，
随后用户要求恢复。q6C整题耗时含执行暂停，整题/请求耗时均不作为比较延迟。这只支持单道独立题内容评分上升，不能声称任务成功、
效率或泛化提升；q6 私有原文、评判条件和原始理由不进入公开文档。

## 录制本地机制检查 / Recorded local mechanism checks

```sh
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-q1-q2-q5-data \
  --unbounded --output /private/NEW_minimax_healthy
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-q1-q2-q5-data \
  --unbounded --failures --output /private/NEW_minimax_failures
```

`97c6cbb` 的 healthy-v3/failure-v3 分别 PASS：57/166 录制调用、0 实际模型 HTTP、
0 基础设施调用。failure 明确回退 K 0。两组重放均为 0 新调用、0 回执/产物写入。
这些是原生内容协议经本地适配的机制检查，合成分数/token 不代表真实质量、节省或完整验收。
旧 v1/v2 失败及修复前日志保留，不反写历史结果。

## 全行报告及实际结果 / All-pair reports and observed results

无需服务器生成 `all-pair-report.json/.csv/.md`，保留全部阶段/组、失败、未启动项、
中间/最终分数、回退与 worker/judge/proposal/Wiki 用量。宏调用单列，每个原语 leaf 展开记账。
缺失质量/token 为 null，不补成 0；评审健康、任务 success、比较完成和最终版本验收分别报告。

当前 q5 A/B/C 均为健康最终评审，三组 success=false，内容分别为 0.6666666667/0.6/0.8；
worker 调用 14/20/30、leaf 27/43/49，均为 3 次原生交互/2 次纠错。C 内容较高，同时
worker 成本增加；B 内容低于 A。q2 的 0.5833→0.75、success 0→0 支持候选选择，
不能掩盖任务失败或成为独立泛化提升声明。完整指标/费用见验收记录。

开发 benchmark 共 218 次实际 HTTP，全部 completed；worker 124、judge 85、proposal 9。
MiniMax 可用性2次、历史 GLM 335 次分别报告。报告的 `acceptance_complete:true` 范围明确为
`local_q5_A_B_C_execution_verification_only`，`quality_improvement_claim:false`；
独立确认新增 89 次 completed HTTP，worker 53/judge 36，1042902 tokens；累计 benchmark 307
全部 completed，加可用性2为 MiniMax 309。q6 报告完成范围也仅为独立执行验证，
不代表任务成功、整个 LIFT 基准或版本验收完成。

## 历史容器与 GLM 边界 / Historical container and GLM boundaries

旧容器实现提供 `ContainerAgentRuntimeAdapter`/`ChatAgent` warmup/evolve/commit/load/holdout，
worker rw/judge ro 无网络容器、`/opt/darwin-state/accepted.json` 层内导出/加载及 leaf trace。
旧 81 录制响应容器/追踪/后处理记录保留在验收报告，它是合成机制证据；不是本轮 MiniMax
Docker/Langfuse 验收。`requirements.lock` 是已解析环境版本清单，不是跨平台 wheel 哈希锁。

历史 GLM 007 仍为 335/500，334 completed、1 unknown，保持暂停；HTTP 200 头不能证明完整
流收到，历史异常原因不可还原。新诊断保存限定异常类型/阶段/状态/流计数与响应 ID 哈希，
完整终止流在后续清理取消中仍保存完成回执；不能据此恢复旧请求或拼接旧质量结果。
本地路线没有本轮 Docker、Langfuse、完整容器追踪或独立轨迹评判，不声明全 LIFT 基准完成。
当前源码 wheel 已通过现有 Python 3.11 环境的源码外安装检查，4 个 Episode、0 模型 HTTP；
SHA 256 `9d3466ec0ef1d7d151d61cbaa57553b26443f426832896520b51cde543c9c32b`。
sdist无私有产物/配置凭据，重建wheel与已安装wheel逐字节一致；打包检查对应文档提交
`5e069e0`。最终文档评审与需求审计待补，协调者随后重建最终文档归档并同步外部分发清单；
没有推送、标签或发布。
