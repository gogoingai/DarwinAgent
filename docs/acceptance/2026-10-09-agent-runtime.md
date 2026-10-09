# 通用 Agent 运行时验收 / Generic Agent runtime acceptance

**0.3.0 本地准备；两次真实诊断失败，修复后效果比较待执行 / Prepared locally; two live diagnostics failed, post-repair comparison pending.**

本报告记录 2026-10-09 隔离分支上的离线验收和两次失败的真实开发诊断，未发布、推送、合并或创建标签。
[机器可读证据](2026-10-09-agent-runtime.json)包含逐文件源码哈希、日志哈希和全部十个 native
单元的无秘密汇总；原始回执、失败、材料与私有配置仍保留在忽略的本地证据目录。

This records offline acceptance of prepared 0.3.0 and two failed live development diagnostics. The linked JSON records source/log hashes and
all ten native cells without task materials, prompts or credentials. Private local evidence
and original failures remain on disk. No publication, push, merge or tag was performed.

| 检查 / Check | 实际结果 / Observed result |
| --- | --- |
| 此前完整回归 / Pre-repair framework at aadb5b4, Python 3.14.4 | 650 discovered, 642 passed, 8 explicit resource skips; 56.309 s |
| 此前 / Retained LoCoMo | 18/18 passed |
| 此前 / Retained TravelPlanner | 11/11 passed |
| 当前运行时与协议回归 / Current Python 3.14 covering tests | 73/73 passed; 9.562 s |
| 当前原生边界 / Current Python 3.12 covering tests | 26/26 passed; 0.681 s |
| 最低版本聚焦回归 / Minimum Python 3.11.15 | 69/69 runtime/episode/ReAct recovery tests passed; 9.824 s (rebuilt wheel) |
| 干净安装 / Clean Python 3.11.15 wheel outside checkout | Passed: site-packages, own-loop + ReAct, 4 artifacts, replay, old doctor/demo/resume and unchanged Wiki |
| Ruff / format | Current 2 touched Python files passed; earlier whole-tree 314-file check retained |
| 此前第三领域样例 / Retained legacy third-domain example | Passed |
| Docker + pinned native LIFT | Passed; 81 recorded responses, 0 model HTTP |
| Native trace readback + postprocessing | Passed; 20 sessions; HTML and two CSVs generated |
| 真实模型内容质量 / Live model content quality | Pending coordinator |
| 轨迹评判 / Trajectory judge | `not_run`; native numeric placeholders are not quality evidence |
| 独立确认 / q6 confirmation | Sealed and unrun |

以上通过项包含提示输出：asyncio 慢任务诊断及预期的旧兼容路径诊断仍保留在日志中。
这些提示不代表断言失败，也不证明实时性能退化；没有修改阈值或为清理输出重跑无关套件。
此前 `aadb5b4` 因宿主身份契约变化执行完整框架回归（642 通过、8 跳过）。
后续两次协议修复仅重跑受影响的覆盖测试、原生机制和安装包验收，没有重跑完整框架套件。

Passing checks retain notices, including asyncio slow-task diagnostics and expected legacy
compatibility diagnostics. Logs and thresholds are preserved; no broad suite was repeated
merely to silence output. The earlier host-identity fix at `aadb5b4` required the full framework run
(642 passes, 8 skips). The two subsequent protocol repairs ran only affected covering tests,
native mechanisms and installed-package acceptance; they did not rerun the full framework suite.

最终 wheel / sdist 摘要见[构建产物记录](2026-10-09-agent-runtime-distributions.json)。
The [distribution record](2026-10-09-agent-runtime-distributions.json) identifies the rebuilt
wheel and sdist; it is recorded separately to avoid embedding an archive's own hash inside it.

具体复现命令见[中文](../zh-CN/agent-runtime-reproduction.md)与[English](../en/agent-runtime-reproduction.md)。

## 跳过项 / Skipped resources

八项沿用现有资源边界，没有新增跳过：明确 HF 原始输入一项；历史归档证据两项；
外部 TravelPlanner 故障回放三项；官方 TravelPlanner 评测器与数据库一项；可选形式验证依赖一项。
这些没有运行，不记为通过。现有启动时间阈值没有修改。此前 `aadb5b4` 完整运行通过了该检查；
此前任务报告中的 Python 3.14 启动时间失败仍保留，不据单次通过宣称性能提升。

The eight existing skips comprise one explicit HF-input test, two archive-evidence tests,
three external TravelPlanner failure-replay tests, one official TravelPlanner evaluator/data
test and one optional formal-dependency test. They are unrun, not passed. Startup thresholds
are unchanged. The earlier full run at `aadb5b4` passed the timing check; earlier task evidence of a
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

当前协议修复后新身份为 `offline-native-live002-repair-final`（此前 final-fix、live-repair 与 task4 的证据均保留），固定 0.3.0 源码后重新运行真实容器、原生评判协议、
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

## 首次真实诊断与协议恢复 / First live diagnostic and protocol recovery

已授权 Coding 端点的 `glm-5.3-flash` 可用性检查成功，使用 1 次 HTTP。随后原生
`live-v030-001` 的 q1 warmup 在 4 次已完成 worker HTTP、3 次已结算工具调用后失败：
第 4 条回复为“计算已验证。正在写入结果文件。”，不是动作 JSON，原 ReAct 直接抛出
`JSONDecodeError`。原始请求已包含 `json_mode=True`，问题不是未请求 JSON 模式，
也不是模型权限或接口不可用。worker 实测 prompt/completion tokens 为 21459/1997。

共享总账共用 5/100 次，5 次均完成，无未知请求或暂停。q2 未运行，q5 A/B/C 均未启动，
q6 未打开，原生后处理因无配对结果失败。CLI 的退出码 0 不代表实验完成。
保留原始回执、材料、失败与全部未启动项，不把失败的 q1 当作内容质量比较。

The authorized GLM Coding availability check succeeded with one HTTP attempt. Native run
`live-v030-001` then failed in q1 warmup after four completed worker responses and three
settled tool calls: response four was a natural-language status sentence, so ReAct raised
`JSONDecodeError`. All four original requests already asked for JSON mode; this was a
settled protocol violation without bounded feedback, not a missing mode or access failure.
Worker measured prompt/completion tokens were 21459/1997. The shared ledger used **5/100**,
all five completed, zero unknowns/pause. q2 was unrun, q5 A/B/C unstarted, q6 sealed. Native
postprocessing failed without pairs; CLI exit zero is not evidence of completion.

第一次协议修复仅在 ReAct 响应解析处捕获 `JSONDecodeError`，保留原响应并返回格式反馈，
后续请求计入相同 8 次上限；显式 JSON 参数只是明确已有契约。已完成写入不重复执行，
未知请求不自动重发，不扩大工具权限、模型范围或预算。独立 Agent 自有循环不变。
录制反例在旧源码表现为 failed/4 模型/3 leaf，在修复后为 completed/6 模型/4 leaf，
均为 0 实际模型 HTTP，仅证明恢复机制。修复后的新原生录制验收通过，81 录制响应、
0 模型 HTTP；没有把这些合成分数和 token 混入真实调用统计。

The first protocol repair fed back only parsing `JSONDecodeError` after a settled reply and keeps
all follow-up requests inside the unchanged eight-request cap. It preserves response receipts,
never executes a partial action, leaves unknown submissions quarantined, and leaves independent
Agent loops unchanged. A recorded counterexample changes failed/4 model/3 leaf to completed/6
model/4 leaf with zero model HTTP; this proves recovery only. Fresh native recorded acceptance
also passes with 81 recorded responses and zero model HTTP. Synthetic metrics stay separate
from the five actual requests.

第一次协议修复的覆盖检查：Python 3.14 运行时/实验/ReAct/未知用量 68 项通过；Python 3.12 原生
材料/工具/传输/ReAct 21 项通过。先前完整 650 项与数据集检查是修复前证据，继续保留，
没有为此次局部修复重复无关套件。当前安装包最低版本检查和分发哈希见机器可读记录。

First protocol-repair covering checks: 68 Python 3.14 runtime/experiment/ReAct/accounting tests and
21 Python 3.12 native data/tool/transport/ReAct tests pass. The earlier full 650-test and
dataset runs are retained pre-repair evidence; unrelated suites were not repeated. Current
minimum-version installed checks and distribution hashes are in the machine-readable records.

## 第二次真实诊断与产物结构恢复 / Second live diagnostic and artifact shape

`669b94d` 上的新运行 `live-v030-002` 在 q1 warmup 使用 5 次已完成 worker 请求、
4 次已结算 leaf 调用，已经写入文件；最终 JSON 将 `artifacts` 表示为字符串数组。
`EpisodeResult` 要求每项是对象，因此交付处出现 `ValueError`。此前共享提示词只给出
`artifacts: []`，没有说明非空条目的结构。worker 实测 prompt/completion tokens 为
29087/1254。两次失败加可用性检查累计 **10/100** 次实际 HTTP，全部完成，无未知请求；
q2 未运行、q5 A/B/C 未启动，q6 继续封存。原文件和写回执保留，不以文件存在宣布任务质量通过。

Fresh run `live-v030-002` on `669b94d` used five completed worker requests and four settled
leaf calls in q1 warmup. The file was written, but final JSON supplied string entries in
`artifacts`; EpisodeResult requires object mappings, so finalization raised ValueError.
The prior shared prompt showed an empty array without describing nonempty entries. Worker
measured prompt/completion tokens were 29087/1254. Both failed runs plus availability total
**10/100 actual HTTP attempts**, all completed, zero unknown. q2 and q5 A/B/C remain unrun;
q6 is sealed. Original files and write receipts remain evidence, not proof of task quality.

当前修复明确工具回复与交付回复结构；产物数组必须由对象构成，文件路径来自真实工具结果，
不增加通用必填字段、不把字符串自动转换成对象。执行前校验自己产生的 `ActionProtocolError`
通过相同有界循环反馈；只在解析与预校验处捕获，不吞掉工具契约、宿主最终检查或 backend 的
任意 `ValueError`。预算仍为 100/8/3，未知请求不重发。录制反例从 failed/5 模型/4 leaf
变为 completed/6 模型/4 leaf，前后都只写一次，续跑不重写。这仍是离线机制证据。

The shared protocol now specifies action/final shapes and object-array artifacts, using actual
tool output paths for files. No universal metadata field is added and no string is silently
converted. Only locally produced prevalidation ActionProtocolError joins parse-error feedback;
tool contracts, host checks and backend ValueError remain outside that catch. Budgets stay
100/8/3 and unknown requests never auto-replay. A recorded counterexample changes failed/5
model/4 leaf to completed/6 model/4 leaf; both write exactly once and replay without another
write. This is offline mechanism evidence only, with zero new real model requests.

## 修复后真实比较待执行 / Post-repair live comparison pending

协调者须在同一全局 100 次 HTTP 总账下记录每个计划单元：阶段、题号、A/B/C、状态、内容
分数、纠错轮数、展开 leaf、原生工具数、worker/judge tokens、耗时、失败、未知、未启动项，
并列出 proposal/Wiki 额外费用和总 HTTP。不能用缺失值替代 0，不能删去下降行。
q5 是开发诊断；修复后需用新身份重跑全部 A/B/C，预算不足即报告 partial。
q6 未运行，不能得出泛化或最优结论。

The coordinator must fill all planned cells, failures/unknowns/unstarted work, full role costs,
evolution overhead and actual shared HTTP attempts. Null means unavailable, never zero.
Repairs require new identities for all A/B/C while retaining the same remaining budget.
The q5 development comparison cannot establish independent quality/generalization; q6 is unrun.

静态检查初次报告 ReAct 导入区多余空行（I001）；原日志保留，修正后 Ruff 通过。
源码字节变化后使用 `offline-native-live002-repair-final` 重新执行原生与 trace 验收，并重建安装包。
The initial Ruff check found I001 (an extra import-section blank line). Its log is retained;
final Ruff passes. The source-byte change triggered fresh native/trace acceptance and rebuilt distributions.
