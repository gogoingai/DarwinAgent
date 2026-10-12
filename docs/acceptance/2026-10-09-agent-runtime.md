# 通用 Agent 运行时验收 / Generic Agent runtime acceptance

**MiniMax q5 与独立 q6 实测已完成，当前 wheel 源码外安装检查通过；sdist 与最终审查待完成。0.3.0 仅为本地准备版本，未发布。**

**MiniMax q5 and independent q6 measurements are complete; the current wheel passed installed checks outside the checkout. Final sdist and review are pending. 0.3.0 is prepared locally and unpublished.**

本页当前证据属于冻结提交 `97c6cbb695384bdd4f0023d68f2baa0bdfd12dce`，
冻结测量源码身份摘要为 `0bd50bf7407d8274cd926e82c193d2e5825af84c54b6d28b5010e45f4122d10a`。
当前层是固定 LIFT 原生内容评判协议的**本地执行适配**。没有为本轮启动 Docker、Langfuse、
完整容器追踪或独立轨迹评判，也没有完成整个 LIFT 基准。
[机器可读记录](2026-10-09-agent-runtime.json)将当前检查、完整开发结果与历史记录分开。

Current evidence is bound to the frozen commit and source digest above. The current layer is a
**LOCAL adaptation** of the pinned native LIFT content-judge protocol. Current MiniMax evidence
contains no Docker, Langfuse, full container trace, independent trajectory judge or full-benchmark
acceptance. The JSON separates current checks, terminal development results and history.

## 当前回归与离线机制 / Current regressions and recorded mechanisms

| 检查 / Check | 冻结源码上的证据 / Evidence on frozen source |
| --- | --- |
| 完整框架 / Framework | 692 discovered; 684 passed; 8 explicit resource skips; 74.053 s |
| LoCoMo | 18 passed |
| TravelPlanner | 11 passed |
| Ruff check / format | Passed; 330 files formatted |
| 无上限本地 healthy-v3 / Healthy recorded pilot | PASS; 57 recorded calls; 0 model HTTP; 0 infrastructure calls; no fallback |
| 无上限本地 failure-v3 / Failure recorded pilot | PASS; 166 recorded calls; 0 model HTTP; 0 infrastructure calls; explicit K 0 fallback |
| 两组重复读取 / Replay of both pilots | 0 new model calls; 0 receipt/artifact writes |
| 当前 wheel 与源码外安装 / Current wheel and installed checks | PASS: current source, Python 3.11 site-packages; 4 episodes; settled replay and legacy checks; 0 model HTTP |

八项资源跳过没有算作通过；原有 asyncio 慢任务提示保留。离线录制的分数和 token 只验证
协议、选择、回退和恢复机制，不能当作 MiniMax 质量或真实调用成本。failure-v3 的 PASS
表示已知失败继续、保留失败并回退 K 0 的机制符合检查，不表示任务成功或进化成功。

The eight resource skips are unrun, not passes; existing asyncio notices remain in the logs.
Recorded scores/tokens establish protocol, selection, fallback and replay mechanics only.
The failure pilot's PASS means failure handling and K 0 fallback satisfy the checks; it does not
mean the failed tasks succeeded or evolution improved quality.

## 当前模型与执行契约 / Current provider and execution policy

各角色固定为 `MiniMax-M3.1-Flash-Preview`，端点 `https://api.minimax.cn/v1`，温度 0，
`thinking.type=adaptive`、`reasoning_effort=max`。显式策略 `minimax-adaptive-v1` 将配置的
生成上限 16384 以 `max_completion_tokens` 发送，不叠加旧推理 buffer；worker、judge、
proposal、Wiki 使用相同策略。没有模型或端点回退。

本次显式选择模型调用无上限：worker/session、worker/turn、judge、proposal、Wiki 与实际
HTTP cap 均为真实 `null` / Python `None`，不是极大整数。调用仍逐次持久化记账；未知请求
或写入仍隔离，不能自动重发。原语权限、每个 Episode 的 64 个 leaf、3 次原生交互
（最多 2 次纠错）、一轮进化及一个候选保持固定。

All roles use the same explicit model/endpoint, temperature 0, adaptive/max policy and generation
16384, sent as `max_completion_tokens` without the legacy reasoning buffer. Model-call caps are
actual null/None under an explicit opt-in. Every call still counts durably; unknown effects remain
quarantined. Primitive permissions, 64 leaves per episode, three native interaction turns,
one evolution round and one candidate remain fixed.

通用默认值仍为 `ExecutionProfile.max_model_calls=8`、`ReActAgent.max_turns=16`、核心生成
4096；旧 `datasets/lift/pilot.example.json` 保持 HTTP 100/worker 8，旧有限预算运行使用原身份。
现有公开 API、旧 v3 bundle 与 graph-QA 兼容性属于回归范围。
Generic defaults remain model 8/ReAct 16/generation 4096; the legacy example remains HTTP 100/worker 8.
Existing finite-budget identities, public APIs, v3 bundles and graph-QA compatibility stay covered.

## 真实开发结果 / Real development results

q2 仅用于候选选择：K 0 的内容分数 0.5833，候选 0.75；success 均为 0。
严格选择理由为 `quality_improvement`，选中内容 ID
`5556a04468fe163d4620703f652820a35e91610d1d818ef2cf476f9e36a80d04`。
实际候选只修改 P 中的 worker 提示。完整 S/F/C/P 空间已向提案开放，但本轮真实候选
没有修改 S、F 或 C，因此不能据此声称已经测量四类资产共同进化的效果。

The q2 selection content metric rose from 0.5833 to 0.75 while success remained 0 in both entries.
Selection accepted a worker-P-only candidate. The complete S/F/C/P proposal space is exposed,
but this real candidate changes no S/F/C asset; q2 selection is not independent quality confirmation.

| q5 组 / Arm | 原生最终内容分数 / Native final content | success | 评审健康 / Healthy | worker 模型调用 / Calls | worker leaf |
| --- | ---: | --- | --- | ---: | ---: |
| A: plain ReAct | 0.6666666667 | false | true | 14 | 27 |
| B: ReAct + K 0 | 0.6 | false | true | 20 | 43 |
| C: ReAct + selected K 1 | 0.8 | false | true | 30 | 49 |

q5 三组均有健康最终评审、各使用 3 次原生交互/2 次纠错，但任务均未达到 success 标准。
B 的内容分数低于 A；C 的内容高于 A、B，同时 worker 模型调用、leaf 与 token 均更高。
这不支持成功率、效率或泛化提升结论。不能用 q2 的采纳理由覆盖 q5 的负面结果。

All three q5 arms have healthy final evaluations and use three native interactions/two corrections,
but none meets the native success criterion. B scores below A; C scores higher while using more
worker calls, leaves and tokens. These results do not establish task-success, efficiency or
generality improvement. q2 selection cannot erase the negative q5 outcomes.

| q5 arm | worker tokens | judge calls | judge leaf | judge tokens | worker request seconds | judge request seconds | native task seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 248186 | 16 | 30 | 215409 | 160.2780 | 94.0174 | 254.5371 |
| B | 357765 | 14 | 29 | 172170 | 242.0006 | 91.4559 | 333.6781 |
| C | 589386 | 11 | 23 | 122752 | 329.1521 | 67.6530 | 396.9978 |

上表保留 worker 与 judge 的独立成本；宏调用 A/B/C 为 0/2/3，宏内部 leaf 不隐藏。
完整物理执行共 7 行，全部 completed/healthy/final、success=false；复用的 q1 准入/训练
收据不会重复计费。q1 基线/候选内容为 0.8333333333333334/0.67，q2 为 0.5833/0.75。
全部行及精确费用保留在 JSON，不能只展示 q5 C 的较高内容分数。
The tables keep worker and judge costs separate. Macro calls A/B/C are 0/2/3 with all leaves counted.
All seven physical execution rows are completed/healthy/final and success=false. Reused q1 admission/
training receipts are not double-counted. q1 baseline/candidate content is 0.8333333333333334/0.67;
q2 is 0.5833/0.75. JSON retains every row and exact cost rather than showing only the best q5 score.

| 开发角色 / Development role | Actual HTTP | Measured tokens | Sum of request seconds |
| --- | ---: | ---: | ---: |
| worker | 124 | 1558114 | 890.2466 |
| judge | 85 | 830758 | 476.4107 |
| proposal | 9 | 144455 | 36.9460 |
| benchmark total | 218 | 2533327 | 1403.6033 |

218 次开发 HTTP 全部 completed，unknown/submitted 为 0；两次 MiniMax 可用性调用单列，
不混入 benchmark。费用包含训练、验证、比较与提案；本轮没有另一个 Wiki 角色模型请求。
请求耗时之和不是整个 campaign 的墙钟耗时；原生 task 耗时、worker 与 judge 请求耗时分别保留。
All 218 development HTTP attempts completed, with 0 unknown/submitted. Two availability requests
remain separate. Costs include training, validation, comparison and proposal; no separate Wiki-role
model request appears in this development ledger. Summed request time is not campaign wall time.

本地开发报告的 `acceptance_complete:true` 仅指
`local_q5_A_B_C_execution_verification_only`；`quality_improvement_claim:false`。
进化流程完成且候选被采纳，与整个版本验收的范围不同。
The local development report's true acceptance flag is explicitly limited to q5 execution
verification, with a false general quality-improvement claim; candidate adoption is a separate result.

## 独立 q6 确认 / Independent q6 confirmation

q6 使用单独确认模式、工作区与材料清单，在开发选择冻结后执行 A/B/C；源码摘要、
模型/请求策略、生成、有效调用 cap、K 0、选中资产和持续计数总账与开发一致。
确认不训练、不调用 proposal/Wiki、不进化。这里只记录协调者提供的安全汇总，不公开
任务原文、私有评判条件或原始 judge 理由。
q6 used a separate confirmation mode, workspace and manifest after development selection froze,
with the same source/model/policy/generation/caps/K 0/selected assets and continuing ledger. It ran
no training, proposal, Wiki or evolution. Only safe aggregate measurements are reported.

| q6 arm | Final content | success | Healthy | Worker calls | Worker leaf | Corrections | Macro calls |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| A: plain ReAct | 0.79 | false | true | 15 | 24 | 2 | 0 |
| B: ReAct + K 0 | 0.86 | false | true | 18 | 28 | 2 | 2 |
| C: ReAct + selected K 1 | 0.93 | false | true | 20 | 59 | 2 | 3 |

| q6 arm | Worker tokens | Judge calls | Judge leaf | Judge tokens | Worker request seconds | Judge request seconds | Native task seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 148603 | 12 | 24 | 127466 | 96.8877 | 59.8884 | 156.9544 |
| B | 209800 | 12 | 21 | 113927 | 143.6020 | 55.1612 | 199.0073 |
| C | 327736 | 12 | 21 | 115370 | 151.0302 | 147.2371 | 298.6358 |

独立单题 q6 中，C 内容分数较 A 高 0.14、较 B 高 0.07；A/B/C 均为健康最终评审，
三组 native success 均 false，均使用 3 次原生交互/2 次纠错。C worker 调用、leaf、token
均更高。因此只能表述为**这一道独立题的内容评分上升**，不能声称任务成功、
效率提高或更广泛的质量/泛化改善。不能把 0.93 写成“93% 成功率”。
On this one independent task, C's content score is 0.14 above A and 0.07 above B. All native success
values remain false and all arms use three interactions/two corrections. C uses more worker calls,
leaves and tokens. This supports **a content-score increase for this one independent task**,
not task success, efficiency improvement or broader quality/generality. 0.93 is not a 93% success rate.

确认新增 89 次 HTTP，全部 completed：worker 53/686139 tokens/387.9643请求秒，
judge 36/356763 tokens/260.5556请求秒；没有 proposal/Wiki 模型请求。
开发218 + 确认89 = benchmark 307 completed，unknown/submitted 为0；benchmark 总 tokens
为3576229、请求耗时之和2052.1231秒。另有2次可用性探测，MiniMax总尝试为309；
可用性 token 未在安全汇总中给出，不补0。旧GLM 335单列。请求耗时之和不是campaign墙钟耗时。
Confirmation added 89 completed HTTP: worker 53/686139 tokens/387.9643 request seconds and
judge 36/356763 tokens/260.5556 request seconds, with no proposal/Wiki model requests.
Development 218+confirmation 89=307 completed benchmark requests,0 unknown/submitted,3576229 tokens
and 2052.1231 summed request seconds. Two availability probes bring total MiniMax attempts to 309;
their token total was not supplied and is not invented as zero. HistoricalGLM 335 remains separate.

q6C 的原生 `task_time_s=298.6357932090759` 包含用户要求的暂停时间，仅保留为原始
观测，不用于无暂停的性能比较。分角色请求耗时之和同样是实际记录的时长，不是 campaign
墙钟时间，也不据此声称性能改善。
q6C native `task_time_s=298.6357932090759` includes a user-requested suspension. It is retained as
an observed field and is not clean performance wall time. Role request-duration sums remain observed
durations rather than campaign wall time and do not support a performance-improvement claim.

## 当前分发与最终准备 / Current distribution and final readiness

当前源码 wheel `darwinagent-0.3.0-py3-none-any.whl` 为336825 bytes、123 members，SHA 256
`9d3466ec0ef1d7d151d61cbaa57553b26443f426832896520b51cde543c9c32b`。
构建检查确认私有产物未打包、配置凭据不在包中；Python 3.11现有验证环境在源码目录外
导入site-packages，完成4个Episode、已结算重放、旧doctor/demo/resume/Wiki检查，0模型HTTP。
The current-source wheel is 336825 bytes with 123 members and the hash above. Private artifacts and
configured credentials are excluded. Its existing Python 3.11 verification environment imported
site-packages outside the checkout and passed four episodes, settled replay and legacy checks,
with zero model HTTP.

真实测量与wheel安装验证已完成，sdist重建、分发清单同步、文档审查与需求审计仍待完成。
机器记录的整体 `acceptance_complete:false` 保留这一准备状态；没有合并、推送、标签、
上游消息或发布。后续分发清单由协调者在文档提交后更新。
Real measurement and wheel installation checks are complete. Final sdist, distribution manifest,
documentation review and requirement audit remain pending, so whole-goal acceptance is false.
No merge, push, tag, upstream message or publication occurred. The coordinator updates the
[distribution record](2026-10-09-agent-runtime-distributions.json) after this documentation commit.

上述源码身份对应实测冻结快照，也包含当时测试夹具。补充原生契约检查初次为39项、
38通过/1错误：手写 confirmation 夹具缺少已有的执行策略身份字段。`528d0e9` 仅修正
`datasets/lift/tests/test_confirmation.py`，没有放宽生产校验；复测39/39通过、0跳过，
8.598秒、0模型HTTP。原始失败日志保留，限定范围审查仍待完成。
The measured source identity includes its then-current test fixtures. The initial supplemental
native contract suite had39tests,38passes and one error caused by missing existing policy fields
in a handcrafted confirmation fixture. Commit `528d0e9` changes only that test fixture, preserves
production validation and passes39/39tests with0skips in8.598seconds and0modelHTTP. The original
failure log is retained; scoped review remains pending.

证明记录核对全部255个非测试源码路径字节不变，生产摘要为
`bb83f6b8966d528a0706a59b05c0f6e68710060e2346011f2a671154b386f2bf`。
包含修正夹具的当前集合摘要为
`ec08e25355eef460262faa97a01756c4bb009612bd6a36cb0def795306a70ef0`；
它不能替换 `97c6cbb` / `0bd50bf7...` 的实测身份。旧运行身份和实测分数未改，
也没有宣称真实调用是在 `528d0e9` 上重新执行。
The proof records all255non-test source paths unchanged with the production digest above. The
fixture-corrected aggregate is `ec08e25355eef460262faa97a01756c4bb009612bd6a36cb0def795306a70ef0`;
it does not replace the measured `97c6cbb`/`0bd50bf7...` identity. Original run identities and scores
are retained; real model execution under `528d0e9` is not claimed.

## 历史 GLM 与报告 / Historical GLM and reports

历史 GLM 总账为 335/500：334 completed、1 unknown，007 仍暂停。HTTP 200 响应头不能证明
完整流式回执；旧诊断不足以确定超时、流中断或本地保存错误中的哪一种原因。本轮新策略
不能修复已丢失的历史异常，也不授权重试、放弃或清除旧暂停。最终安全汇总核对5436个
历史文件，mismatches=0。MiniMax 新账本、2 次可用性和质量结果分别报告，不与旧 GLM 成绩拼接。

Historical GLM remains 335/500 attempts, 334 completed and one unknown; run 007 stays paused.
The cause of its incomplete receipt cannot be established retrospectively. The new policy does
not resolve that request or authorize replay/abandonment. The final safe summary verifies 5436 historical files with 0 mismatches. MiniMax quality, its new ledger and the two availability probes remain separate.

以下保存旧 26/100、`68a0084` 及更早源码的验收原文。旧段落中的“当前”“最终”和请求限制
均指那个历史快照；不能用作 `97c6cbb`/MiniMax 的当前状态。JSON 同样保留旧对象的全部字段。
The following is the preserved earlier 26/100 report. Its “current”, “final” and budget wording
refers only to that historical snapshot, not to 97 c 6 cbb/MiniMax. The complete old JSON is retained.

---

# 通用 Agent 运行时验收 / Generic Agent runtime acceptance

**0.3.0 本地准备、未发布；真实验收部分执行，现因单题预算暂停 / Prepared, unpublished; partial live acceptance paused at the task cap.**

本报告记录 2026-10-09—10 隔离分支上的离线验收、两次协议失败与第三次预算暂停，未发布、推送、合并或创建标签。
[机器可读证据](2026-10-09-agent-runtime.json)包含逐文件源码哈希、日志哈希和全部十个 native
单元的无秘密汇总；原始回执、失败、材料与私有配置仍保留在忽略的本地证据目录。

This records offline acceptance of prepared 0.3.0 and three live development attempts: two protocol failures, then a task-budget pause. The linked JSON records source/log hashes and
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
| 真实开发诊断 / Live development diagnosis | Partial, paused: 26/100 completed HTTP; q1 initial score 0.33 is intermediate only; no final score or A/B/C comparison |
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

## 第三次真实诊断与最终暂停 / Third live diagnostic and final pause

`live-v030-003` 使用冻结源码 `68a0084`。q1 初次交付完成 5 次 worker 请求、4 次 leaf；
原生内容评审完成 8 次请求，返回 success=false、score=0.33。这是**初次交付的中间结果**，
不是最终题目分数。公开反馈指出 CSV 交付格式不符合要求，需改为 Markdown 并在首行标注日期。
随后纠错完成另 3 次 worker 请求、3 次 leaf，第 9 个本地模型预留在 HTTP 提交前被累计
8 次 worker/task/session 上限拒绝。暂停原因是 `budget_exhausted`；未清除暂停或提高任何上限。
不能据此断言算法劣于其他组或框架演化失败：其他组和演化均未启动。

On frozen source `68a0084`, q1 first delivery completed five worker requests and four leaves.
The native content judge used eight requests and returned success=false, score=0.33 for that
**intermediate first delivery only**. Public feedback requested Markdown with a first-line date
instead of the delivered CSV. Correction consumed three more worker requests and three leaves;
the ninth local reservation was rejected before HTTP by the cumulative eight-request worker cap.
The stable `budget_exhausted` pause remains. No final task score, algorithm comparison or evolution
conclusion follows from this unfinished correction.

| 实际 HTTP / Actual HTTP | 请求 / Requests | 实测 tokens / Measured tokens | 请求耗时之和 / Sum of request elapsed seconds |
| --- | ---: | ---: | ---: |
| 可用性检查 / Availability | 1 | 65 | 2.1895 |
| 三次运行 worker 合计 / Workers across three runs | 17 | 101410 | 117.6193 |
| LIVE003 原生内容评审 / Native content judge | 8 | 143064 | 391.4317 |
| 总计 / Total | 26 | 244539 | 不作为整题耗时 / Not a task wall-time total |

全部 26 次 HTTP 均完成，未知提交为 0；组成是可用性 1 + LIVE001 4 + LIVE002 5 + LIVE003 16。
LIVE003 单独为 worker 8 次、47613 tokens、60.9758 秒；judge 8 次、143064 tokens、391.4317 秒。
proposal/Wiki 均未启动，额外请求/token 为 0。共享 100 次总账尚余 74，不抵消单题额度耗尽，
也不构成继续调用授权。原始失败、产物、总账和暂停状态保留。

All 26 HTTP requests completed, with zero unknown submissions: availability 1 + LIVE001 4 +
LIVE002 5 + LIVE003 16. LIVE003 alone used worker 8/47613 tokens/60.9758 s and judge
8/143064 tokens/391.4317 s. Proposal and Wiki were unstarted, with zero request/token overhead.
The remaining 74 shared requests do not override the exhausted task cap or authorize continuation.
Original failures, artifacts, ledger and pause are retained.

原生行记录 worker model_calls=9、pending_calls=1，包含未提交的本地预留；这不是 9 次 HTTP。
其 worker_tokens 仍为 null，known subtotal=47613；该行的 unknown_token_requests=1 表示
预留核算未闭合，不是未知 HTTP。实际已完成 HTTP 的全局 token 总计 244539 可独立核对。
原生 `task_time_s=0.006783962249755859` 被重试覆盖，仅保留为产物字段，不能作为真实整题耗时。
上表使用模型请求实测耗时；没有可用的整题墙钟耗时值。

The native row counts nine reservations and one pending local reservation, not nine HTTP calls.
Its worker token total remains null with known subtotal 47613; the single unknown-token entry
reflects incomplete reservation accounting, not an unknown HTTP submission. Completed HTTP
usage independently totals 244539 tokens. Native `task_time_s=0.006783962249755859` was overwritten
by retry and is only an artifact field. Request elapsed durations above are measured; full task
wall time is unavailable.

| 计划阶段 / Planned stage | 最终状态 / Final status | 最终质量 / Final quality |
| --- | --- | --- |
| q1 warmup | paused; correction unfinished | null |
| q1 K0 admission/train | unstarted | null |
| q2 K0 validation | unstarted | null |
| proposal/evolution | unstarted | null |
| q1 K1 admission/train | unstarted | null |
| q2 K1 validation | unstarted | null |
| candidate selection | unstarted; K0 unchanged | null |
| q5 A | unstarted | null |
| q5 B | unstarted | null |
| q5 C | unstarted | null |
| q6 independent confirmation | sealed, unrun | null |
| trajectory judge | not_run | null |

上表明确列出逻辑计划阶段，不补造原生单元。JSON 保留实际原生报告的四行（q1 与 q5 A/B/C）。
没有候选被验证，也没有最终分数、质量提升或全阶段验收完成结论。缺失质量不是 0。

The table lists logical planned stages, not fabricated native cells. JSON retains the four actual
native report rows (q1 and q5 A/B/C). No candidate was validated, no final score or improvement
was measured, and all-phase acceptance is incomplete. Missing quality is not zero.

协调者零模型调用回读通过两条已完成 session：judge 8 generations/4 leaf、worker turn 0
5 generations/4 leaf。未完成 worker turn 1 的 3 次请求/3 leaf 留在本地 journal/总账，
未 flush 到 trace-state；因此 trace 不覆盖全部 8 次 worker 请求。原生后处理因无配对结果失败，
异常保留，没有真实 CSV/HTML；CLI 退出 0 不代表实验完成。前文 CSV/HTML 仅来自离线录制夹具。

A zero-model-call readback verified completed judge (8 generations/4 leaves) and worker turn 0
(5 generations/4 leaves) traces. Unfinished worker turn 1 retains its three requests/three leaves
in local journals/ledger but has not flushed to trace-state; trace coverage is incomplete.
Native postprocessing failed without pairs, with its exception preserved and no live CSV/HTML.
CLI exit zero does not mean completion; the CSV/HTML mentioned earlier belongs only to the fixture.

本次最终交付仅更新文档和 sdist，wheel 字节及 129 个冻结源码文件不变。73/26/69 项覆盖检查
属于 `68a0084` 协议修复，完整 642 项通过属于此前 `aadb5b4`；没有为文档更新重跑无关套件。
This final delivery updates documentation and sdist only; wheel bytes and all 129 frozen source
files remain unchanged. The 73/26/69 covering checks belong to the `68a0084` repair; the full
642 passes belong to earlier `aadb5b4`. No unrelated suite was repeated for this documentation update.

静态检查初次报告 ReAct 导入区多余空行（I001）；原日志保留，修正后 Ruff 通过。
源码字节变化后使用 `offline-native-live002-repair-final` 重新执行原生与 trace 验收，并重建安装包。
The initial Ruff check found I001 (an extra import-section blank line). Its log is retained;
final Ruff passes. The source-byte change triggered fresh native/trace acceptance and rebuilt distributions.
