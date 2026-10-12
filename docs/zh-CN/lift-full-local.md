# MiniMax 全量 LIFT 本地测评

[English](../en/lift-full-local.md)

本路线覆盖固定 EALE 版本的 14 个场景。它在本地复用 LIFT 原生任务执行、内容评审以及轨迹评审提示和解析，不需要 Docker 或 Langfuse。代码为可选集成，不随核心 wheel 安装；需要仓库源码、现有 Python 3.12 环境和固定上游依赖。

## 协议

LIFT 固定为 `9d282e415c5e3b0f65b82c8758a013b269583d93`，EALE 固定为 `419789ed1662e7b9ad16aa6620a4b31449612948`。全量原始任务为 56 个训练题和 28 个测试题。

每场景 q1/q2/q3 产生训练轨迹和进化依据；q4 验证候选，只有汇总指标可进入提案器。所有场景完成选版并冻结后，再执行 q5/q6 的三组比较：A 为最小 ReAct，B 为同一 Agent 加 K0，C 加选中的 K1；候选拒绝则 C 明确使用 K0，仍单独执行。

因此必需覆盖为 56 个训练/验证单元和 84 个 A/B/C 测试单元，合计 140。候选准入、额外验证和进化请求另行记账。三组同模型、同工具权限，每题最多 30 轮原生交互。该协议与历史 3 轮销售诊断分开，成绩不能拼接。

基础工具为列文件、读文本、读表格、表格计算、写文件、网页搜索、网页读取。组合函数展开后的底层调用单独记录。网页能力不执行 JavaScript，也不提供任意宿主机代码执行；依赖这些能力的任务可能失败，失败仍计入完整矩阵。

## 运行与恢复

当前 CLI 延续已经存在的 MiniMax 共享预算目录，拒绝绕开未知请求重新创建账本。它要求原策略、回执和累计 HTTP 计数完整且没有待确认请求。首次新增轨迹评审角色时生成独立的兼容登记，原策略和历史回执保持原字节。这条恢复路线不提供空白预算目录的自动初始化。

复制 [配置示例](../../datasets/lift/minimax.full.example.json)，填写私有数据目录、全新运行目录和原预算目录。数据目录需要逐文件固定哈希的 `full-download-manifest.json`；下载对象应与固定上游 Git/LFS 对象核对。私有运行时注入 `DARWINAGENT_API_KEY`，不将密钥写入配置、仓库或模型上下文。

以下命令会产生真实请求；执行前应授权模型、预算和联网工具：

```sh
DARWINAGENT_MODEL=MiniMax-M3.1-Flash-Preview \
DARWINAGENT_BASE_URL=https://api.minimax.cn/v1 \
PYTHONPATH=src /path/to/existing/python3.12 -m datasets.lift.full \
  --upstream /path/to/pinned/LIFT \
  --config /private/full-config.json \
  --run-id NEW_FULL_RUN
```

固定温度 0、adaptive thinking、reasoning_effort=max 和 max_completion_tokens=16384。示例的实际模型请求上限为 null，需要显式允许无限调用；它不代表零成本。执行、构图、Wiki、提案、内容及轨迹评审共同累计到原实际请求账本。

恢复使用相同命令、配置和运行标识。终态回执直接复用，不重新执行或发送请求；已知单题失败记录后继续其他题；结果未知则暂停共享入口。源码、配置、工具或输入变更后必须新建实验身份，三组重新运行。不能通过恢复改写原始失败或混合成绩。

## 判读结果

运行目录中的 `full-matrix-report.json/.csv` 包含所有必需单元、未开始项、失败、原生内容成功/评分、单独轨迹评分、纠错、展开工具调用及各角色成本。当前运行成本与共享账本历史成本分开。

`coverage_complete` 表示单元齐全且均有终态；`evaluation_healthy` 表示评测证据完整；`c_quality_satisfied` 要求所有 C 测试单元原生 success=true、content_score=1。只有全场景覆盖、健康、C 达标、当前运行真实付费回执核验通过且没有共享暂停时，`acceptance_complete` 才能为 true。录制客户端和离线合成分数不能通过真实验收。

销售 q5/q6 已在历史诊断中观察过，必须单列；不得将其当作新的独立泛化证据。完成执行不等于证明进化收益，宣传还应展示 A/B/C 差异、全部退化和额外成本。
