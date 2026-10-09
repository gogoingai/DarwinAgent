# 通用 Agent 运行时验收 / Generic Agent runtime acceptance

**0.3.0 本地准备，真实模型诊断待执行 / Prepared locally; live diagnosis pending.**

本报告记录 2026-10-09 隔离分支上的离线验收，未发布、推送、合并或创建标签。
[机器可读证据](2026-10-09-agent-runtime.json)包含逐文件源码哈希、日志哈希和全部十个 native
单元的无秘密汇总；原始回执、失败、材料与私有配置仍保留在忽略的本地证据目录。

This is offline acceptance of prepared 0.3.0. The linked JSON records source/log hashes and
all ten native cells without task materials, prompts or credentials. Private local evidence
and original failures remain on disk. No publication, push, merge or tag was performed.

| 检查 / Check | 实际结果 / Observed result |
| --- | --- |
| 核心回归 / Framework, Python 3.14.4 | 650 discovered, 642 passed, 8 explicit resource skips; 56.309 s |
| LoCoMo | 18/18 passed |
| TravelPlanner | 11/11 passed |
| 新运行时、实验与原生边界 / Native-focused Python 3.12.13 | 74/74 passed; 11.093 s |
| 最低版本聚焦回归 / Minimum Python 3.11.15 | 60/60 runtime/episode/ReAct recovery tests passed; 9.471 s (rebuilt wheel) |
| 干净安装 / Clean Python 3.11.15 wheel outside checkout | Passed: site-packages, own-loop + ReAct, 4 artifacts, replay, old doctor/demo/resume and unchanged Wiki |
| Ruff / format | Passed; 314 Python files already formatted |
| 第三领域旧样例 / Legacy third-domain example | Passed |
| Docker + pinned native LIFT | Passed; 81 recorded responses, 0 model HTTP |
| Native trace readback + postprocessing | Passed; 20 sessions; HTML and two CSVs generated |
| 真实模型内容质量 / Live model content quality | Pending coordinator |
| 轨迹评判 / Trajectory judge | `not_run`; native numeric placeholders are not quality evidence |
| 独立确认 / q6 confirmation | Sealed and unrun |

以上通过项包含提示输出：asyncio 慢任务诊断及预期的旧兼容路径诊断仍保留在日志中。
这些提示不代表断言失败，也不证明实时性能退化；没有修改阈值或为清理输出重跑无关套件。
本轮因宿主身份契约变化执行一次完整框架回归，并重跑受影响的原生、恢复和安装包验收。

Passing checks retain notices, including asyncio slow-task diagnostics and expected legacy
compatibility diagnostics. Logs and thresholds are preserved; no broad suite was repeated
merely to silence output. This fix required one framework regression and refreshed affected
native, recovery and installed-package acceptance.

最终 wheel / sdist 摘要见[构建产物记录](2026-10-09-agent-runtime-distributions.json)。
The [distribution record](2026-10-09-agent-runtime-distributions.json) identifies the rebuilt
wheel and sdist; it is recorded separately to avoid embedding an archive's own hash inside it.

具体复现命令见[中文](../zh-CN/agent-runtime-reproduction.md)与[English](../en/agent-runtime-reproduction.md)。

## 跳过项 / Skipped resources

八项沿用现有资源边界，没有新增跳过：明确 HF 原始输入一项；历史归档证据两项；
外部 TravelPlanner 故障回放三项；官方 TravelPlanner 评测器与数据库一项；可选形式验证依赖一项。
这些没有运行，不记为通过。现有启动时间阈值没有修改。本次最终完整运行通过了该检查；
此前任务报告中的 Python 3.14 启动时间失败仍保留，不据单次通过宣称性能提升。

The eight existing skips comprise one explicit HF-input test, two archive-evidence tests,
three external TravelPlanner failure-replay tests, one official TravelPlanner evaluator/data
test and one optional formal-dependency test. They are unrun, not passed. Startup thresholds
are unchanged. This final full run passed the timing check; earlier task evidence of a
Python 3.14 timing failure remains historical and is not erased or recast as a speed gain.

## 本次发现与复测 / Failures and retests

1. 首次完整检查：645 项、8 跳过，安装元数据仍为 0.2.0，源码为 0.3.0，版本一致性断言失败。
   执行 `uv sync --frozen --group test` 更新工作环境元数据，没有修改断言，完整重跑通过。
   首次日志 `task4-framework.log` 与最终 `task4-framework-final.log` 均保留。
2. 首次原生聚焦检查将测试模块名误写为单数 `tests.test_episode_experiment`，产生导入错误。
   更正为现存的复数 `tests.test_episode_experiments` 后 65 项通过；两份日志保留。

The first full run failed a source/distribution version assertion because editable metadata
still said 0.2.0. Refreshing the frozen environment, without changing assertions, produced
the passing full rerun. A focused-test command initially misspelled the episode test module;
correcting the invocation produced 65 passing tests. Both failed logs remain hashed evidence.
Neither was a reason to relax checks or modify legacy behavior.

## 最终审查修复 / Final review fixes

宿主检查现在将所属对象声明的配置纳入身份摘要。相同配置继续回放；允许策略改为拒绝策略后，
旧运行报身份变化，新运行被拒绝；对应实验身份也变化。私有配置不会以明文进入持久化记录。
原生单元报告核对模型预留、响应和事件。首个请求未知及完成 13 tokens 后下个请求未知时，
worker/judge 总量均为 `null`，已知小计分别为 0 / 13。尚无 EpisodeResult 的轮次读取现存
回执；缺少回执时计数不可用。真正未启动的单元仍显式保留零请求和 `unstarted` 状态。

Host-check owner declarations now bind policy configuration to replay and campaign identity,
using digests only. Native worker/judge totals require matching model reservations, responses
and events. Unknown requests yield null totals and separate known-token subtotals. Unfinished
turns use existing durable steps, with unavailable counts when receipts are missing; genuinely
unstarted cells remain explicit zero-request records. No request is redispatched by reporting.
Before/after counterexamples and original failures remain in the local evidence directory.

## Native 机制结果 / Native mechanism results

本次修复后新身份为 `offline-native-final-fix`（保留 `offline-native-task4-v030` 原证据），固定 0.3.0 源码后重新运行真实容器、原生评判协议、
演化、delta 载入、trace 回读。81 条录制响应由 worker 48、judge 32、proposal 1 构成；
无 Wiki 模型请求，没有实际模型 HTTP。所有十个计划单元均完成，未知/待定调用为 0。
完整十行、各角色用量和耗时见 JSON，不仅保留 q5 成功摘要。

| q5 arm | 合成 success/content | 纠错 / Corrections | 原语 / Leaf calls | 合成 worker tokens |
| --- | --- | --- | --- | --- |
| A: ReAct | true / 1.0 | 1 | 4 | 180 |
| B: ReAct + K0 | true / 1.0 | 1 | 4 | 180 |
| C: ReAct + loaded K1 | true / 1.0 | 0 | 3 | 90 |

These values are **synthetic fixture outcomes**, not measured model quality or live token
savings. Real local tool/container durations are retained separately; they do not measure
network model latency. All ten cells completed, with zero unknown/pending calls. Worker 48,
judge 32 and proposal 1 recorded responses account for all 81; Wiki made no model request.
Fresh-delta load receipts, hook idempotency, source identity and trace evidence are hashed.
New failure fixtures also cover material/symlink rejection before container startup,
cumulative costs across interrupted turns, judge failure and unstarted arms.

## 真实诊断报告待填项 / Pending live report

协调者须在同一全局 100 次 HTTP 总账下记录每个计划单元：阶段、题号、A/B/C、状态、内容
分数、纠错轮数、展开 leaf、原生工具数、worker/judge tokens、耗时、失败、未知、未启动项，
并列出 proposal/Wiki 额外费用和总 HTTP。不能用缺失值替代 0，不能删去下降行。
q5 是开发诊断；修复后需用新身份重跑全部 A/B/C，预算不足即报告 partial。
q6 未运行，不能得出泛化或最优结论。

The coordinator must fill all planned cells, failures/unknowns/unstarted work, full role costs,
evolution overhead and actual shared HTTP attempts. Null means unavailable, never zero.
Repairs require new identities for all A/B/C while retaining the same remaining budget.
The q5 development comparison cannot establish independent quality/generalization; q6 is unrun.
