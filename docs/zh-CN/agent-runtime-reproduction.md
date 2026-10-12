# 复现通用 Agent 验收

[English](../en/agent-runtime-reproduction.md) · [SDK](agent-sdk.md)

本页对应本地准备、尚未发布的 0.3.0，冻结源码为 `97c6cbb`。
当前 MiniMax 运行通过**本地执行适配**复用固定 LIFT 的任务执行与内容评判协议。
q5 与独立 q6 A/B/C 实测已完成，当前 wheel 源码外安装检查通过；
sdist打包检查已通过，最终文档审查和需求审计仍待完成。
[验收记录](../acceptance/2026-10-09-agent-runtime.md)区分当前实测与历史证据。

## 核心回归与实际安装包

以下命令用于复现检查，实际结果单独记录：

```sh
uv sync --frozen --group test
uv run --frozen --group test python -m unittest discover -s tests -v
uv run --frozen --group test python -m unittest discover -s datasets/locomo/tests -v
uv run --frozen --group test python -m unittest discover -s datasets/travelplanner/tests -v
uv run --frozen --group test ruff check .
uv run --frozen --group test ruff format --check .
uv run --frozen python examples/agent_runtime.py --output runs/NEW-agent-offline
uv build --wheel
```

协调者在 `97c6cbb` 上的记录：完整框架发现 692 项，通过 684 项、明确跳过资源项 8 项；
LoCoMo 18 项、TravelPlanner 11 项通过；Ruff 与 330 文件格式检查通过。原日志与诊断提示
保留，跳过项不计为通过；回归未调用真实模型。当前 wheel 已在现有 Python 3.11 验证环境
于源码目录外通过，完成 4 个 Episode、0 模型 HTTP。复现时将新建 wheel 安装到
干净的 Python 3.11 环境，在源码目录外运行
`/path/to/clean/python /absolute/checkout/scripts/check_installed.py`。
该脚本检查 site-packages 导入、自有 Agent 与 ReAct 循环、真实文件、已完成回执复用，以及
旧 doctor/demo/resume/Wiki 行为。旧 wheel 哈希或早期 73/26/69 覆盖检查不能证明当前安装包可用。

## 公开 API 与显式取消模型调用上限

`AgentAdapter.run(task, context)` 由适配器自行控制循环，返回 `EpisodeResult`。
`AgentTask` 提供指令、公开参数与附件；`AgentContext` 提供 model/call/view/check；
`ExecutionBackend` 绑定执行、宿主策略与持久回执。`EpisodeResult.artifacts` 必须是对象数组。
完整构造方式见 [SDK 文档](agent-sdk.md)。

通用默认值仍为 `ExecutionProfile.max_model_calls=8`、`ReActAgent.max_turns=16`、核心生成 4096。
显式 `None` 只取消对应的模型调用或决策次数上限：

```python
from darwinagent.agent import ExecutionProfile, ReActAgent

profile = ExecutionProfile(
    "explicit-model-unbounded", ("read_text", "write_text"),
    max_model_calls=None, max_leaf_calls=64,
)
agent = ReActAgent(max_turns=None, max_tokens=16384)
```

这些对象本身不选择模型提供方、不注册工具，也不取消宿主检查。S/F/C/P 资产仍受固定能力
与生命周期契约约束。每次调用继续持久计数，未知请求/写入不自动重放。旧 v3 bundle、
graph-QA 路径、有限预算与默认执行身份保持兼容；旧有限上限 K 0 不能直接挪用于新的 null 执行。

## 当前 MiniMax 本地复现策略

使用现有 Python 3.12 环境及固定 LIFT 提交
`9d282e415c5e3b0f65b82c8758a013b269583d93`。开发材料为固定 EALE
`419789ed1662e7b9ad16aa6620a4b31449612948` 的 q1/q2/q5 清单；开发中 q6 保持未打开。
本地路线不需要启动 Docker、Langfuse、新服务或安装依赖。

复制 [minimax.local.example.json](../../datasets/lift/minimax.local.example.json)，填入独立私有
运行、材料和预算路径。MiniMax 使用新的预算身份；历史 GLM 总账及未知暂停保留。在同一
MiniMax 身份内，各授权角色、进程和新的比较目录共享原总账。凭据仅由宿主
`DARWINAGENT_API_KEY` 提供；模型/端点环境变量必须与配置完全一致。以下真实模型命令
仅由已获授权的协调者执行：

```sh
DARWINAGENT_MODEL=MiniMax-M3.1-Flash-Preview \
DARWINAGENT_BASE_URL=https://api.minimax.cn/v1 \
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.local \
  --upstream /path/to/pinned/LIFT --config /private/minimax-local.json \
  --run-id UNIQUE_MINIMAX_DEVELOPMENT_RUN
```

显式策略 `minimax-adaptive-v1` 使用通用路由（`model_profiles=False`），
`thinking.type=adaptive`、`reasoning_effort=max`、温度 0；worker、judge、proposal、Wiki
生成上限均为 16384。JSON 配置键为 `max_tokens`，实际 MiniMax 请求键为
`max_completion_tokens`，不叠加旧推理 buffer，也不回退模型或端点。通用及历史注册提供方
沿用原请求 token 约定。

`unbounded_model_calls:true`、`worker_limit:null`、`http_cap:null` 是显式选择。
worker/session、worker/turn、judge、proposal、Wiki 的模型调用上限为真实 None/null，
不是极大整数。3 次原生交互（最多 2 次纠错）、原语权限、每个 Episode 的 64 个 leaf、
一个候选和一轮进化保持固定。SDK retries 仍为 0；已提交未知请求仍关闭共享闸门。
取消调用数量上限不会取消持久计账、请求时限、未知写隔离或身份验证。
旧 [pilot.example.json](../../datasets/lift/pilot.example.json)保持 HTTP 100/worker 8。

本地原语为 `list_files`、`read_text`、`read_table`、`transform_tables`、`write_text`，
限制在独立题目工作区。judge 仅有读工具；不开放任意宿主代码或子进程。私有评判条件、
凭据、选中资产与账本均在工作区外。关闭 dotenv 自动发现与原生 pre-chat 上报。
原生内容评判提示、条件、解析和计分通过本地适配保持权威；结果 JSON 作为字符串放入
外层 `final_response`，没有替换为自制评判器。

## 本地录制机制检查

使用新的输出目录。以下命令只消耗录制响应，不请求真实模型：

```sh
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-q1-q2-q5-data \
  --unbounded --output /private/NEW_minimax_healthy
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-q1-q2-q5-data \
  --unbounded --failures --output /private/NEW_minimax_failures
```

冻结源码 healthy-v3/failure-v3 检查通过，分别为 57/166 次录制调用、0 模型 HTTP、
0 基础设施调用。healthy 选中夹具候选；failure 继续已知失败并显式回退 K 0。
两组重放均为 0 新模型调用、0 回执/产物写入。合成分数与 token 只证明机制，
不代表 MiniMax 质量或节省。早期 v1/v2 失败继续保留，不能将 v3 通过反写成旧轮次通过。

## 选择、开发比较与独立确认

相同任务、kernel、身份的 q1 公开证据可在 warmup/准入/训练间复用。q2 私有验证不进入
训练 Wiki。沿用 CampaignController/EpisodeExperiment/ProposalSession/Wiki，开放完整
S/F/C/P 提案能力；不支持的声明及固定权限变更无法准入。只提交一个候选。
即使收到重复证据提示或修正复制的续页字段，proposal/Wiki 调用及原查询也保留计数。
原件与旧存储游标继续可读；8000 与 512 的序列化回复边界检查不改变源文字节。

A 为原始 ReAct，B 加 K 0，C 加选中 K 1 或显式 K 0 回退。选择冻结后，q5 A/B/C 独立执行；
C=K 0 也不复用 B。本轮真实候选只改 worker P；q2 内容 0.5833→0.75、success 0→0
支持候选采纳，不是独立质量提升证据。q5 A/B/C 评审健康且为最终结果，但均 success=false，
内容分别为 0.6666666667/0.6/0.8；worker 调用 14/20/30、leaf 27/43/49，C 执行成本更高。
三组均为 3 次原生交互/2 次纠错。负面结果必须保留。

开发调度结束后，协调者才准备独立 q6 清单与新目录。设置 `mode:confirmation`，
`selection_run_root` 指向已完成开发目录，`data_root` 使用 q6 专属材料。
沿用冻结源码、模型/请求策略、生成配置、实际 cap、总账、K 0 和选中资产。
该模式独立执行 q6 A/B/C，不训练、不调用 proposal/Wiki、不进化。本轮冻结 MiniMax
确认已完成：A/B/C 健康最终内容评分为 0.79/0.86/0.93，native success 均 false，
各使用 3 次交互/2 次纠错；worker 调用 15/18/20、leaf 24/28/59。C 的 worker 调用、
token、leaf 更高。协调者误将旧GLM的暂停范围用于MiniMax，随后用户要求恢复；
q6C整题耗时包含执行暂停，整题/请求耗时均不作为比较延迟。
因此只支持这一道独立题的内容评分上升，不支持任务成功、
效率或泛化提升。安全指标和全部费用见验收记录，q6 私有文本/评判条件不公开。

## 结果、失败与历史边界

`all-pair-report.json/.csv/.md` 保留全部计划组、最终/中间分数、失败、未启动项、回退及
worker/judge/proposal/Wiki 实测调用/token/耗时。宏和可选 1–16 动作批量中的每个 leaf
单独计数；整批先校验契约，再执行，已知失败停止剩余项。ReAct 保留一份最新视图，
按顺序保留观察与不可变回执。

健康的原生评审也可以 success=false；完整比较也可以没有提升或发生下降。缺失最终质量
与 token 为 null，不能补成 0；中间分数不能代替最终结果。开发 benchmark 为 218 次 completed HTTP，worker 124/judge 85/proposal 9，
2533327 tokens、请求耗时之和 1403.6033 秒；MiniMax 两次可用性请求单列。报告完成范围
开发报告范围仅为 q5 执行验证，quality_improvement_claim=false；独立 q6 执行验证
与打包检查也已完成，最终文档审查/需求审计待补。确认新增 89 次 completed 请求（worker 53、judge 36），
1042902 tokens、请求耗时之和 648.5198 秒；累计 benchmark 307 + 可用性2 = MiniMax 309，
benchmark 总 tokens 3576229。历史 GLM 335 单列。

已知单题失败可结算后继续独立调度；模型回执未知、不确定写入和适用共享闸门会暂停
新请求，不自动重放。新 MiniMax 诊断只保存限定阶段/类型/状态/流计数与响应 ID 哈希；
完整终止流在清理故障或取消中保留已结算回执。这些修复不能重建旧 GLM 007 的故障原因。

历史 GLM 为 335/500（334 completed、1 unknown），保持暂停且未改动。早期 26/100、
容器/追踪/81 录制响应记录已归档至验收报告；它们属于独立源码身份，不能描述当前 MiniMax
验收。本地路线没有本轮 Docker/Langfuse/完整容器追踪或独立轨迹评判结果，不能称为完整
容器或全基准复现。没有合并、推送、标签、发版或向上游发送消息。真实测量与当前 wheel
验证与sdist检查通过：无私有产物/配置凭据，sdist重建wheel与已安装wheel逐字节一致。
检查对应文档提交 `5e069e0`；最终文档审查/审计待补，协调者随后重建最终文档归档。
当前wheel SHA256为
`9d3466ec0ef1d7d151d61cbaa57553b26443f426832896520b51cde543c9c32b`。
