# DarwinAgent 原生 LIFT 集成 / Native LIFT integration

本目录是可选的 Python 3.12+ 基准集成，不进入核心 wheel。调用固定上游原生 CLI、`LIFTPipeline`、`ContainerAgentRuntimeAdapter`、`run_task` 内容评判、warmup/commit/load/holdout 生命周期。注册 wrapper 不修改上游源码。

- LIFT：`9d282e415c5e3b0f65b82c8758a013b269583d93`。
- EALE：`419789ed1662e7b9ad16aa6620a4b31449612948`；本地 `download-manifest.json` 校验每个文件。
- q1：训练/行为准入；q2：独立候选验证；冻结选中版本后 q5：开发比较。q6 不下载、不读取、不执行。
- A：同一 ReAct 与固定原语，无 kernel；B：K0；C：采纳的 K1，拒绝则 K0。所有 worker/judge 使用同一用户指定模型、端点、temperature=0，保持独立历史。
- 每题最多 3 次原生 worker↔judge 回合，即最多 2 次原生纠错；整个 worker session 累计最多 8 次模型请求。共同的实际 HTTP attempt 上限为 100，SDK retries=0、传输最大尝试次数为 1。
- `budget_root` 必须独立于每次比较的 `run_root`。框架修复后新建比较目录，但复用预算目录、HTTP 总账和未知请求暂停状态。`submitted` 未收据请求在恢复时全局隔离；禁止自动清除暂停文件或重发未知请求。

## 运行与边界

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
