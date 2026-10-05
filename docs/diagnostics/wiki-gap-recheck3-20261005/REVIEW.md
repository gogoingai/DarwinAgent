# Wiki 第三次复查（2026-10-05）

结论：大参数覆盖和旧归因超长故障已有实证修复，全拒 C/容器误判 C 也被正常答案正例拦住；但正例的语义合法性、verified_fix 全场景绑定、诊断事实保留仍未过关。暂不建议合并。

本次没有修改活跃框架源码、冻结数据、历史评分或运行状态，没有合并/提交/停止活跃运行，没有真实模型请求或全量题目评测。仅新增本目录审查脚本和报告；官方评分额外验证了任务中声明的一个正例。

## 通过的验证

- 框架 315 项，OK（跳过 2 项）；LoCoMo 14 项、Travel 11 项，OK。
- 原 loop3 的 B0 formal、R1/R2 formal/decision 五条归因记录，现在全部形成请求并写入归因。请求字符数分别为 19196、29656、30062、27110、27516，均低于 35000。采用 RecordedClient，仅验证工程路径，不证明模型归因质量。
- 新电池：IDEAL_C 通过；拒绝全部正常答案 C、冻结数组误判 C 均失败。原来只测弃答的问题已堵住，但见下方正例合法性问题。
- 新压力样本确实含“乔恩＋空类型＋空日期＋limit500”；原坏 F 在压力场景即耗尽 30000 步，能在准入时被拦。整套回归另覆盖原 F 拒绝、修复 F 同预算通过；没有扩大执行预算。
- structure 行 ok=False 不再直接把旧误拒标成已验证修复。
- 复现前后相关源码/任务文件指纹一致；旧审查脚本与结果未覆盖。

## 三项仍需修复

| 优先级 | 问题和证据 | 修复与验收要求 |
|---|---|---|
| P1 | **正常正例并未验证语义合法，重新出现“正确 C 被必过正例挡住”。** tasks/travel_planning/task.yaml 的 legal_plan 是旧模型输出，只有类型/天序/非空城市验证；官方评分实测 final0，住宿城市/资源校验和缺失景点检查不通过。加入官方已有“非转移日须有餐饮”的规则后，C 被 answer_ex_legal_plan must_pass 挡住。该 Rockford 三天计划还被包装进每个 case 的第一题和首行证据；train:1 的目的地是 Pensacola、日期也不同。 | 正例必须带独立且一致的问题、参数、数据/证据与验证范围；不能把旧模型输出称作已验证语义正例后要求任何 C 接受。可用任务层自建小型可验证夹具作为专用检查案例，或生成与当前 case 参数/图一致的已验证正常答案。只证实结构合法的留 structure 档；任务语义正例须过对应确定性校验，不使用评测标准答案。验收：正常语义检查 C 能过，全拒/容器错误 C 被挡；换目的地、日期或图时不会把旧行程错装成该题答案。不要以删除餐饮/证据/任务约束来换过门。 |
| P2 | **输入绑定只对三个场景前缀生效。** wiki.py 的 REF_BOUND 只有 replay/check_replay/answer_ex；base/stress/wide_filter 等仍只按资产ID＋场景名验证。原“换 input_ref”反例仍生成 admission_verified；新增生产形式的 F stress 反例也如此：原 A→B 的调用失败，仅证明 A→C 成功，旧故障便被标已修复。 | 所有数据相关场景都绑定原输入，不按前缀豁免。保留 case/问题、参数 digest、快照 digest、图身份及原检查/工具ID；无足够输入证据时保持待验证。对任务固定负例也应绑定完整样本身份，避免图/问题变化后的同名假复现。真正静态检查可单独定义静态规则身份。验收：同一 stress/base 场景不同参数/不同case不算修复，原输入成功才算；保留已修复的 structure 排除和 replay 场景绑定。 |
| P1 | **归因摘要仍会删除关键故障事实。** 最新 loop6 R1 attempt-0 的 C 拒绝原因为 structured_answer is missing or not parseable JSON；真实维护器 request 的对应场景行已经丢失 issues/ok/expectation/error_type，只剩 status=failed、error=null。R1 同一 answer_2 拒绝累计出现8次，不能据此确定反复原因，但维护器确实拿不到该场景的拒绝理由。另一个5000字符预算反例中，第三级压缩结果4314字符，却把真实形态的 candidate 检查事件压成仅 {stage:candidate}，checks 的检查ID/问题/步数和 candidate_summary.json_type 都丢失。 | `_brief_facts` 保留失败 C 的 issues、ok、expectation、error_type/步数预算，失败签名应包含规范化后的 issues。三级压缩按生产嵌套结构处理：缩 checks[] 文本/数量但保检查ID、ok、issues、步数；保 candidate_summary.json_type；保 observation 中故障预算事实及失败前调用。先压原文、答案、代码摘要等，再保留诊断骨架；不能直接按顶层键白名单丢嵌套事实。验收：真实 loop6 失败场景摘要仍能说明拒绝原因；5000字符及默认预算下保留关键签名/类型/步数，旧五条归因继续通过。 |

## 当前真实循环

最新 loop6 的源码冻结指纹一致。最终 B0、R1、R2 都 complete，各 completed1/1、generation_faults0、evaluation_faults0；三阶段 final/macro_cs/macro_hc 均0。两轮因 primary_not_strictly_improved 正确拒绝，决策归因均已回写；整体 summary complete。证明有健康循环链路，不证明 Wiki 已带来任务收益。

loop5 是旧失败记录（B0被冒烟门拦截），不作为最终代码验收。README 顶部“全部修复”、底部把 loop3 当健康完整验收的勾选仍需随最终结果修正。main 合并后的三组至少5轮验证尚未执行。

## 证据和复现

- `recheck.py` / `counterexamples.json`：沿用上次独立反例并传入新增 answer_examples，结果不覆盖旧审查。
- `additional_probes.py` / `additional_counterexamples.json`：一个声明正例的官方评分、餐饮检查的准入反例、stress 输入错配反例、强压缩嵌套事实反例。
- `brief_fact_probe.json`：最新真实 C 失败行和维护摘要的字段差异；真实维护请求也已读到同样缺失。
- `run_snapshot.json`：真实运行采样。

执行时在 Wiki 工作树根目录使用本项目 Python 和 PYTHONPATH=.。这些探针输出证据，不进行真实模型答题、不更改活跃实验。

建议一次补齐上述三项，再将本目录反例转成红→绿回归。保留现有运行证据，源码固定后开新目录做小规模多对话与 Travel 正式循环；验证健康、经验正确回流和后续方向可引用，再交由用户指定 agent 合并和执行主干三组至少5轮验证。验证/测试集不得回流 Wiki 或提案。
