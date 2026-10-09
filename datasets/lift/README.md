# DarwinAgent 原生 LIFT 集成 / Native LIFT integration

本目录是可选的 Python 3.12+ 基准集成，不进入核心 wheel。调用固定上游原生 CLI、`LIFTPipeline`、`ContainerAgentRuntimeAdapter`、`run_task` 内容评判、warmup/commit/load/holdout 生命周期。注册 wrapper 不修改上游源码。

- LIFT：`9d282e415c5e3b0f65b82c8758a013b269583d93`。
- EALE：`419789ed1662e7b9ad16aa6620a4b31449612948`；本地 `download-manifest.json` 校验每个文件。
- q1：训练/行为准入；q2：独立候选验证；冻结选中版本后 q5：开发比较。q6 不下载、不读取、不执行。
- A：同一 ReAct 与固定原语，无 kernel；B：K0；C：采纳的 K1，拒绝则 K0。所有 worker/judge 使用同一用户指定模型、端点、temperature=0，保持独立历史。
- 每题最多 3 次原生 worker↔judge 回合，即最多 2 次原生纠错；整个 worker session 累计最多 8 次模型请求。共同的实际 HTTP attempt 上限为 100，SDK retries=0、传输最大尝试次数为 1。
- `budget_root` 必须独立于每次比较的 `run_root`。框架修复后新建比较目录，但复用预算目录、HTTP 总账和未知请求暂停状态。`submitted` 未收据请求在恢复时全局隔离；禁止自动清除暂停文件或重发未知请求。

## 本地执行 / Local execution

当前推荐直接本地运行：不启动 Docker、Langfuse、容器、后台服务，也不下载依赖或创建新环境。
它复用固定上游的 `execute_task`、`run_task`、内容评判提示与解析协议；这是本地执行适配，
不等同于完整原生容器生命周期复现。下面保留的容器路线仅作为历史记录。

使用现有 Python 3.12 环境、已有固定上游与 16 文件数据清单，沿用 `pilot.example.json`。
`run_root` 采用新目录；`budget_root` 指向原始独立的 100 请求账本，不能重置。
凭据只从宿主 `DARWINAGENT_API_KEY` 获取；模型、端点环境变量必须与 JSON 完全一致。
当前授权模型为 `glm-5.3-flash`，所有角色温度为 0，8/3/100 三项限制不可提高。
以下真实运行命令仅供获得模型调用授权的协调者使用：

```sh
DARWINAGENT_MODEL=glm-5.3-flash DARWINAGENT_BASE_URL=AUTHORIZED_CODING_ENDPOINT \
/path/to/existing/python3.12 -m datasets.lift.local \
  --upstream /path/to/pinned/LIFT --config /private/local-pilot.json --run-id UNIQUE_LOCAL_RUN_ID
```

本地工具沿用五项原语及其输入输出；每个实例拥有独立工作区根路径，不启动子进程，
不提供任意宿主代码执行。judge 只注册读工具，直接调用写入也会拒绝。私有评判、已选资产、
传输凭据与账本不在工具可访问工作区内。启动时关闭 dotenv 自动读取和原生 pre-chat 上报。
每次执行前验证工程源码、数据及传输身份，源码变化必须另建 A/B/C 比较目录。

q1 的相同任务/K0/固定身份收据复用于 warmup、准入和训练；候选 q1 的准入/训练也复用同一
公开证据。q2 仍独立验证，最多提交一个联合 S/F/C/P 候选，并沿用核心严格选择。
训练失败时跳过 proposal/Wiki，仍诊断 q2；进化不可用时显式选择 K0。
冻结选择后，q5 A/B/C 各自独立执行，C=K0 也不会复用 B 的运行。
单题已知失败或 8 次 worker 上限产生可重放终态，继续下一题；未知请求/写入或全局 100 上限暂停。
历史歧义暂停仅由协调者调用 `SharedTransport.recover_local_cap_pause` 审计恢复，启动器不会自动恢复。

无需服务器即可生成 `all-pair-report.json`、`.csv`、`.md`，保留失败、未开始、缺失值、
中间评判以及 worker/judge/proposal/Wiki 各角色的实际费用。最终内容分数与失败前的中间分数
分别展示；宏调用数单列，原生工具计数及展开计数均按实际原语 leaf 计数。`evaluation_healthy` 与任务终态
分开；`acceptance_complete` 只有真实传输、完整有效的 q5 A/B/C 和身份验证通过才可能为真。
录制机制测试永远不会成为真实验收，回退及 q6 未测范围会写入报告。

离线录制试跑（不连接基础设施、不调用真实模型）：

```sh
/path/to/existing/python3.12 -m datasets.lift.tests.offline_local \
  --upstream /path/to/pinned/LIFT --data /private/pinned-data --output /private/NEW_local_pilot
# 失败继续与 K0 回退：在新目录运行相同命令，并加 --failures；全失败加 --all-failed。
```

Local mode uses existing dependencies and the pinned native worker/judge protocol, without containers,
Langfuse, service calls, dotenv discovery or child tool processes. Use the module command above with a
fresh run directory and the original independent budget directory. Identical public q1 evidence may
be reused across warmup/admission/training; q2 remains private validation, and q5 A/B/C always execute
independently after selection freezes. Known local failures continue; unknown effects and the global
100-attempt gate pause. Reports disclose fallback, null/intermediate scores, all role costs and the
unopened q6 scope. This is a local adaptation, not full native container reproduction; recorded tests
do not establish real model quality or complete real acceptance.

## 历史容器运行与边界 / Historical container route

使用独立 3.12 环境安装本目录 `requirements.lock` 与当前核心源码；不修改核心 `uv.lock`。锁文件是实际验收环境的已解析版本清单，非跨平台 wheel 哈希锁。Docker 镜像仅包含固定 Python 基础镜像与标准库工具：

```sh
DOCKER_CONTEXT=colima-darwin-lift docker build -t darwinagent-lift-tools:task3 datasets/lift
```

不切换全局 Docker context。容器仅挂当前题工作区，worker 为 rw、judge 为 ro，无网络和凭据。原语包括 `list_files`、`read_text`、`read_table`（CSV/XLSX 首表）、`transform_tables`（内存 SQLite 只读 SELECT）、`write_text`。SQL 可聚合、连接、转换和排序；XLSX 日期序列值保持数值，SQLite 可用 `date(value + 2415018.5)` 转换。路径、符号链接和写入均限制在 `/workspace/task`。

接受的 kernel、训练 Wiki、选择记录哈希及镜像证据在清理前导出。`/opt/darwin-state/accepted.json` 位于容器可提交层，fresh evolved worker 实际读取并校验内容 ID。judge 始终使用基线工具镜像。

warmup hook 调用现有 `CampaignController`、`EpisodeExperiment`、`WikiService`、`ProposalSession`。其 6 次独立的 q1/q2 准入、训练和验证运行都是原生 `execute_task`，费用全部计入共享总账。原生 warmup q1 是额外训练轨迹，不冒充这 6 次运行之一。只有训练原文和用户式反馈进入 Wiki；q2 的原始评判留在宿主评测目录。

先复制 `pilot.example.json`，填入维护者提供的模型/端点和独立目录。模型凭据只通过宿主 `DARWINAGENT_API_KEY` 环境变量提供。以下命令会进行真实模型调用，限获得授权的协调者执行：

```sh
DOCKER_CONTEXT=colima-darwin-lift DO_TRAJECTORY_JUDGE=false \
DARWIN_LIFT_CONFIG=/private/pilot.json \
/path/to/python3.12 -m datasets.lift.run --upstream /path/to/pinned/LIFT \
  -r darwinagent --run-id UNIQUE_RUN_ID --repeat 1 \
  --warmup-container-policy serial_single --holdout-container-policy serial_multi \
  --holdout-phase-policy serial --max-parallel-suites 1 --max-concurrent-tasks 1 \
  --max-conversation-turns 3
```

wrapper 从固定数据自动生成仅 q1 warmup / q5 holdout 的原生 suite；不要自行传 `--suite` 或 `--benchmark_dir`。q2 验证发生在 warmup hook 的受控 campaign 中。`DARWINAGENT_MODEL`、`DARWINAGENT_BASE_URL` 必须与配置完全相符。完整后处理需先在宿主设置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`，固定上游的 `trace.list/get` 需要 Langfuse dual-write 兼容模式。

`DO_TRAJECTORY_JUDGE=false` 强制关闭上游独立轨迹模型；原生 CSV/HTML 中的 trajectory 数字是上游占位值，统一按 `trajectory_judge=not_run` 解读。权威全行报告为 `all-pair-report.json`，保留 A/B/C、失败与回归，不把缺失 token/耗时当作测量 0。原生工具计数为实际容器原语调用，宏展开的每个 leaf 都有真实子 span；本 adapter 的 native tool count 与 flattened leaf count 因而相同。proposal/Wiki 成本单独记录。观察故障保留已完成响应并暂停后续调度。

## 离线验收

```sh
/path/to/python3.12 -m unittest tests.test_react_recovery datasets.lift.tests.test_tools datasets.lift.tests.test_transport -q
/path/to/python3.12 -m datasets.lift.tests.http_budget_probe --output /private/NEW_http_probe
DOCKER_CONTEXT=colima-darwin-lift DO_TRAJECTORY_JUDGE=false \
/path/to/python3.12 -m datasets.lift.tests.offline_native \
  --upstream /path/to/pinned/LIFT --data /private/pinned-data --output /private/NEW_native_run \
  --trace --trace-env /private/infra/.env
/path/to/python3.12 -m datasets.lift.tests.trace_probe \
  --upstream /path/to/pinned/LIFT --root /private/NEW_native_run --trace-env /private/infra/.env
```

`--trace-env` 读取本地初始化项目的 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY/SECRET_KEY`，只设置当前进程环境。离线 native 测试使用录制回复：实际 Docker、原生内容评判协议、工具、候选采纳、delta、trace 与后处理均运行，但任务分数、token 和质量差异是合成机制证据，不构成实模型质量结论。候选只改 F，P 字节保持一致。HTTP probe 调用本地模拟 SSE 服务，3 个独立进程/比较目录共用一个 cap=1 的账本，第 2、3 次发送被拒绝。

Source changes require fresh A/B/C identities. Preserve previous artifacts; reuse the independent global budget directory. This adapter is an optional, host-controlled native LIFT integration, not an independent optimizer or replacement judge.
