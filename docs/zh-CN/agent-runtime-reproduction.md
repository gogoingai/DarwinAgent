# 复现通用 Agent 验收

[English](../en/agent-runtime-reproduction.md) · [SDK](agent-sdk.md)

本页对应本地准备、尚未发布的 0.3.0。常规检查使用录制或本地响应；原生 LIFT 与独立轨迹
评判属于不同验收层。[验收记录](../acceptance/2026-10-09-agent-runtime.md)给出实际结果和边界。

## 核心回归与安装包

```sh
uv sync --frozen --group test
uv run --frozen --group test python -m unittest discover -s tests -v
uv run --frozen --group test python -m unittest discover -s datasets/locomo/tests -v
uv run --frozen --group test python -m unittest discover -s datasets/travelplanner/tests -v
uv run --frozen --group test ruff check .
uv run --frozen --group test ruff format --check .
uv run --frozen python examples/agent_runtime.py --output runs/NEW-agent-offline
uv build
```

创建干净的 Python 3.11 环境、安装本地 wheel，在源码目录外运行
`/path/to/clean/python /absolute/checkout/scripts/check_installed.py`。
该脚本确认导入来自 site-packages，检查两种 Agent 循环、真实文件、已完成回执复用，以及
旧 doctor/demo/resume 与 Wiki 字节不变。原生集成另用已验证的 Python 3.12 环境，核心不导入
基准依赖。历史外部资源和形式验证资源缺失造成的跳过不能计为通过；保留完整日志，不放宽
已有启动时间阈值。

## 原生录制响应验收

固定 LIFT 提交 `9d282e415c5e3b0f65b82c8758a013b269583d93`，在独立 Python 3.12 环境
安装 `datasets/lift/requirements.lock`。材料使用 EALE
`419789ed1662e7b9ad16aa6620a4b31449612948` 的 q1/q2/q5 和逐文件固定清单，
不打开、不准备 q6。先准备本地 trace 服务，凭据留在版本库外。
[适配器文档](../../datasets/lift/README.md)说明能力、限制与注册方式。显式指定 Docker context：

```sh
DOCKER_CONTEXT=colima-darwin-lift docker build -t darwinagent-lift-tools:task3 datasets/lift
DOCKER_CONTEXT=colima-darwin-lift DO_TRAJECTORY_JUDGE=false \
/path/to/python3.12 -m datasets.lift.tests.offline_native \
  --upstream /path/to/pinned/LIFT --data /private/pinned-data \
  --output /private/NEW-native-run --trace --trace-env /private/infra/.env
/path/to/python3.12 -m datasets.lift.tests.trace_probe \
  --upstream /path/to/pinned/LIFT --root /private/NEW-native-run --trace-env /private/infra/.env
```

不切换全局 Docker context。该夹具实际运行容器、工具、上游内容评判协议、演化钩子、
新容器加载 delta、trace 回读与后处理，但回复为 81 条录制内容，实际模型 HTTP 为 0。
分数与 token 为合成数据，本地耗时不代表网络模型延迟。原生报告中的轨迹占位值统一按
`trajectory_judge=not_run` 理解。

## 有界真实开发诊断：待执行

代码审查与新源码冻结后，由协调者执行真实模型请求。已授权配置是 `glm-5.3-flash`、
temperature 0、Coding 端点 `https://open.bigmodel.cn/api/coding/paas/v4`，各角色保持一致。
每题最多 3 次交互（2 次纠错）、每个 worker task/session 累计 8 次请求，只允许一轮演化和
一个候选。worker、judge、proposal、Wiki、可用性探测、各进程和新运行目录的实际 HTTP
尝试共享一个稳定的 100 次总账。`budget_root` 独立于 `run_root`，改源码不能重置预算。

A 是原始 ReAct，B 加 K0，C 加实际选中的 K1（拒绝候选则保留 K0）。q1 训练与准入，q2
验证，q5 仅作开发诊断。比较前固定代码、模型、backend、kernel、评测器、数据、权限与
逐题预算。q5 后如有修复，保留原失败，使用新身份、剩余预算重新运行全部 A/B/C；
预算不足就报告未完成。q6 保持封存，独立质量确认尚未运行。

权威报告 `all-pair-report.json` 保留每个计划单元的 arm/phase、状态、success/content、
纠错轮数、展开后的 leaf 与原生工具计数、分角色模型调用/token、实测耗时、失败与未知值。
未完成的 turn 和未启动的 arm 也保留。proposal/Wiki 额外费用、全局 HTTP 单独列出，
缺失 token 为未知。上游筛选后的汇总不能替代全行报告，也不能隐藏下降或错误。
已提交但未知的请求暂停后续传输，明确处理前不能静默重试或删除暂停状态。

本地 `.superpowers/`、材料、密钥与原始评判证据保留在磁盘上；仅将无秘密的汇总事实和哈希
写入跟踪文档。每项缺陷记录复现命令、原始失败、原因、限定范围的修复和覆盖复测，
原始产物保持不变。离线夹具不支持真实质量或泛化提升结论。
