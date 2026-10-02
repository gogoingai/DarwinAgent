# Oak 迭代交接任务清单

日期：2026-10-02。工作目录：`/Users/xu/git/oak`。用户自行安排其他 agent；没有启动新 agent 或全量运行。

## 验收边界

框架修复按运行契约、资产边界、续跑可靠性和已验证的移植能力验收。**评测外错题率 <3% 仅属于后续迭代的收敛目标，不是框架修复的验收条件**；当前小样没有达到该目标，不表示框架修复因此未完成。尚未实现的框架扩展按其自身功能另列。

## 当前基线

- S/F/C/P/H 已接入，H 为 harness。本轮主要契约修复及独立安装验收完成；70 个离线测试通过（新增 26、原 LoCoMo 16、原旅行规划 28）。安装包环境另有 18 个契约/函数测试和第三领域示例通过。
- 发行包：`dist/oak_repro-0.2.0-py3-none-any.whl`。完整自动学习控制器、任意领域模块独立执行、资产增删与自动采纳门控仍是扩展事项，见 `PORTABLE-USAGE.md`；不要报告为已完成。
- r4 固定训练小样：15 题全部完成，无执行/评测故障；修复 gold 精确 **14/15**，原始 gold 精确 **12/15**；评测外错误 **1/15（6.67%）**，未达标。这些小样已用于调试，不是独立验证结果。
- 最后补上了无 gold 的 `QuestionInput` 边界和严格续跑检查。r4 对应其旧源码快照；下一轮用 **r5**。r5 已冻结准备，并预置 r4 的有效模型响应缓存，没有启动模型；预算、答案与报告未复制。
- 固定划分：训练 conv-26/30/41/42/43/44 共 **1157** 题；验证 conv-47/48 共 **429** 题；测试 conv-49/50 共 **400** 题。按整段对话互斥划分，不按成绩换题。
- 11 个原评测代码/数据文件的摘要在 `datasets/locomo/runs/portable_v1/evaluation_frozen.json`。评分、归因、协议、日期解释、gold 和修复表禁止修改。

## 顺序任务

- [ ] **1. 核对运行身份。** 本轮代码保存在当前工作区，未提交，另有原有改动；不要重置或从旧 HEAD 当作新版本。使用现有 `.venv`；验证 11 个评测摘要和 r5 的 `generation_contract.json`。不覆盖旧轮、split 或 `selected.json`。变更生成资产后创建 r6、r7 等新轮次；不要边跑边改正在使用的资产，也不要并发启动多个完整流程。
- [ ] **2. 跑全量训练基线。** 先跑 r5 的 1157 题，保留答案、checks、轨迹、原始/修复 gold 成绩及归因。15 题可做快速回归，不能替代全量训练和集合成绩。
- [ ] **3. 优化所有判错类型。** 即使被归因为“评测内”，也继续优化生成内容、目标要素、粒度与承诺强度；提升与否由同一冻结评测实际验证，归因不代替成绩。当前优先反例为训练 conv-26 **idx14**：问“是否还会想从事该职业”，答案以“不一定”起头，又换成“是否坚定”，弱化了结论。修复通用推断答案组织规则，不写入逐题 gold/答案表。保留主体与事件限定、干净拒答、具体状态和列举完整性的回归。每个补丁标明 S/F/C/P/H；引擎修复不能被记录为内核学习收益。
- [ ] **4. 优化运行速度。** 当前使用原生 fast 抽取/ReAct、strong 合成、独立 strong 限定核查；候选生成关闭思考，独立核查保留思考。核查偶有正文预算被思考耗尽的问题，从训练集比较上下文大小、候选数、核查长度及生成模型策略。评测模型、请求策略和重试协议保持原样。比较精确得分、类别表现、评测外错误、故障数和成本，不能只比较归因数字。
- [ ] **5. 用完整验证选型。** 跑 429 题；不把验证逐题参考答案注入运行策略。严格 <3% 对应最多 **12** 题评测外错误，且全部完成、无执行/评测故障。检查主得分与关键类别退化后再选最终版本。
- [ ] **6. 冻结后跑独立测试。** 完整验证选定版本后执行 `--select`，再跑测试 400 题。严格 <3% 对应最多 **11** 题评测外错误，且全部完成、无故障。测试不反馈本轮调参；未达标如实报告，不据测试错题修改同轮后继续声称独立测试。
- [ ] **7. 输出可核查报告。** 先写“做了什么、结果如何、核心问题”。保留原始/修复 gold；补充宽松/精确双指标时调用现有冻结协议，不修改规则。旧 `lenient_report` CLI 路径不适用于 campaign，不直接照搬。争议题不能扣出分母以宣称达到目标。

## 运行入口

基线与验证；修改生成资产后换新轮次：

```bash
cd /Users/xu/git/oak
.venv/bin/python -u -m datasets.locomo.pipeline.campaign --split train --round r5 --mode coverage --fast-profile native
.venv/bin/python -u -m datasets.locomo.pipeline.campaign --split validation --round r5 --mode coverage --fast-profile native
```

选定最终版本后才执行：

```bash
.venv/bin/python -m datasets.locomo.pipeline.campaign --select --round r5 --mode coverage --fast-profile native
.venv/bin/python -u -m datasets.locomo.pipeline.campaign --split test --round r5 --mode coverage --fast-profile native
```

验证评测冻结：

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from oak.runtime import verify_files
verify_files(Path.cwd(), json.loads(Path('datasets/locomo/runs/portable_v1/evaluation_frozen.json').read_text()))
print('evaluation frozen: verified')
PY
```

主要证据：r4 的 `conv-26/smoke/report.json`、`failures.jsonl`、`system_side.json`、`answers.jsonl` 和 `frozen_code/`；新基线为 r5 的 `generation_contract.json` 及 `frozen_code/`。全部位于 `datasets/locomo/runs/portable_v1/`。失败轮继续保留，修复记录见 `docs/FRAMEWORK-REPAIR-LOG.md`。
