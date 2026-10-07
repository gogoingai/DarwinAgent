# 真实小题验收与发布门槛

日期：2026-10-07。仅使用用户指定的 OpenAI Chat Completion 连接和 `glm-5.3-flash`；生成、审查、提案、Wiki 均使用同一模型，真实 HTTP 并发 1。四个训练型 case、十一题，使用预置中文原文和事实图、登记资产及独立评测器；没有调用嵌入或抽取模型，不代表全量数据集成绩。

## 三项目标与验收

|目标|实际核对|
|---|---|
|人工干预不清空循环|修改并发继续，成功题没有新增调用；保存模型响应后注入中断，再从同一 request_id 恢复；分叉复用零调用；Wiki 调整预算和协议后保留旧响应与同一会话|
|分层 Wiki 不丢原件|保存 q3 全部轨迹、参数、控制数据、节点和原文；初始视图之外的记录可分页读取。完整 130 条跨页链可在原件中核对，读取节点与返回节点分开|
|提案随时找 Wiki，重新归纳|原会话实际发起 query_wiki/regroup，Wiki 对原件分块调用模型并合并；回复固定快照、附引用与覆盖。完成 6 块原件归纳和 5 个合并组，原会话最终 no_change；人工反证后理由明确修正|

## 真实题目与独立结果

|case|题数|核对结果|
|---|---:|---|
|smoke-case|5|4 回答、1 合法拒答；主体林、首日 2026-09-01、130 条、D-17/林与 D-18/赵分别归属、缺失 D-999。原文及轨迹人工核对，另外保存有限字段检查|
|subject-date|2|D-32/赵/2026-03-09、D-31/林/2026-01-02，JSON 目标字段 2/2|
|historical-refusal|2|D-51 在 2026-07-03 为李，不混入前日王；无 D-59 记录合法拒答，2/2|
|paging-tail|2|75 条记录，最后日期 2026-03-16、陈，2/2；计数题明确每页 25 条|

原五题的 ExactFixtureEvaluator 逐字比较整个正文，真实自然语言回答与短标准字符串不同，**原 fixture_exact 为 0/5，未修改、未覆盖，也不报告成 5/5 精确匹配**。另存 factual-audit.json 的原文/轨迹核对与有限字段检查；q1 的日期描述只覆盖所引前 20 条，不代表其独立完成全部日期范围核对。额外六题从一开始使用 JSON 字段协议和独立字段评测，6/6；标准值只在控制器评测器中使用，没有给提案器或 Wiki。

q3 实际链为 device_lookup 返回 20 条，再 f_page_facts(offset=20, limit=20) 返回 20 条，next_offset=40、more_remain=true；然后 offset=40、limit=100 返回 90 条，next_offset=130、more_remain=false。两次分页 matched_count=130，答案引用 130 个不同节点；底层各读取 130 节点，实际返回分别 20/90，未混淆读取与返回。

## 实际发现的失败、原因与复测

1. 提案器连续把动作写成 query_wiki 包装对象，缺少 action，三次有界格式修正未能恢复。原因是错误反馈只说明校验失败，没有给可直接使用的动作格式。补充 action 协议示例及 cursor 必须为 null/string 的提示；保留原三次响应，在原会话登记协议修订，下一次真实响应成为合法 query_wiki。对应 test_proposal_session。
2. Wiki 三次返回完整供应商回执但正文为空，completion_tokens=4096，推理字符约 1.1–1.5 万。属于有回执的协议失败，不是结果未知。增加独立 wiki_max_tokens 配置，在真实复测使用 12000，同时限制归纳 JSON 大小；已完成题目不重做，旧空响应留存。对应 test_explicit_wiki_completion_budget_reaches_chunk_and_merge。
3. 模型沿用 cursor 却改写 question，固定快照校验正确拒绝，但异常曾退出提案会话。将可处理的 Wiki 查询错误保存为 failed 回复，提供原 query 及 continuation_query，允许同一会话修正。跨查询 cursor 仍拒绝，未弱化权限或快照规则。实际会话修正后读完原始分页并产生 no_change。对应 test_proposal_session 中无效 cursor 恢复反例。
4. Wiki 输出有正文、引用也指向真实原件，但字段写成 cite；校验器只支持 ref/evidence_refs，旧提示没有说明字段差异。提供具体条目格式、允许引用及修正提示，任意其他编号继续拒绝。旧失败尝试留存，原 job 显式新尝试继续。对应 test_wrong_citation_field_gets_concrete_repair_schema。

每一项实际失败报告、完整响应和后续尝试都在原运行目录，不靠切换模型或删除检查点消除反例。没有把 no_change 当执行失败，也没有要求冒烟分数必然提高。

## 续跑、迁移与费用记录

subject-date 注入点在工具模型响应已经持久化之后；恢复保留原 request_id，完整回执重用。三个 case 改并发后成功复用均新增 0 次 HTTP；分叉也新增 0 次。实际传输始终串行。

通过 export_run/import_run 把额外三 case 从实现 worktree 导入主目录；143 个原生对象、351 个文件进度对象，缺失 0、冲突隔离 0。活跃锁和被凭据过滤器排除的外部源码文件列在导出清单，不携带活动控制或密钥；原文件字节仍留在源目录。主目录使用公共 Pipeline 继续六题，独立字段仍 6/6，新增调用 0。

HTTP 台账、prompt/completion tokens、主动时间分别保存；增加主动预算保留历史消耗，离线暂停不计入。没有供应商货币账单，不把 token 计数换算为金额。具体最终数值见 intervention-wiki-verification.json 和本地原始报告。

## 离线与包门槛

核心 515 项（7 项条件跳过）、LoCoMo 18 项、TravelPlanner 11 项通过。Ruff 与差异检查通过；最新 wheel 在仓库外安装验收通过，含 doctor、两轮 demo、采纳/工作候选及成功继续，模型 HTTP 为 0。发布前在合并后的 main 再跑对应回归；远端提交编号必须与本地 main 一致。

原始运行目录：runs/intervention-five-live-glm53、runs/intervention-extended-live-glm53。目录不进入 Git；脚本、脱敏汇总和本文件进入 Git，完整回执留本地便于续跑。公开结果不包含密钥。

真实动态验收已完成；本文件记录发布前已通过的门槛。main 的实际提交和远端一致性以 Git 发布核对为准。

另定位并修复大回复的展示顺序：完整合并结论现在放在分块结果之前，首屏可看到跨块结论，后续 cursor 与原件仍完整。真实刷新使用已有六块和五组合并结果，Wiki 新调用为 0；同一提案会话读到完整合并视图并继续。对应 test_large_regroup_first_page_exposes_complete_cross_chunk_conclusion。

最后人工审查发现提案理由把“每页行数等于 limit”泛化为事实，但末页是 90<100。原错误作为 proposal_hypothesis 保存，Wiki 增加引用真实原件的 refutation，按 topic 查询返回原判断与纠正，原会话接受反证并明确撤回。未更换模型、重做题目或重新调用 Wiki 模型；这是提案归纳错误，不是题目答案或分页工具错误。human-refutation.json、proposal.json 和旧报告保留全部记录。

实际 HTTP 109 次（五题与动态会话 76，额外三 case 33），主动执行 46.48 分钟。所有已保存 model ledger 均为 glm-5.3-flash；货币费用未知。当前工作资产 5f9f3093…、adopted=null，本次没有宣称任何候选已证明更好，也没有未处理的未知模型请求。

原五题与动态会话也已导入主目录：216 个原生对象、411 个文件，缺失 0、隔离 0；五题复用、固定 Wiki cursor 续读和带反证的完成提案均新增 0 调用。迁移校验初次误用了纠正后的新查询（新证据快照依法需要重新归纳），其 RecordedClient 未发起 HTTP；该校验尝试已明确放弃并记录。随后改用保存的 cursor，正确复用旧快照，无未知真实请求。新查询看到新纠正、旧 cursor 保持旧快照是预期行为。
