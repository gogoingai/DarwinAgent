# Wiki 缺口修复后的运行审查（2026-10-05）

结论：存在合并前必须解决的问题，当前不能认定 Wiki 验收通过。审查仅读取现有代码/轨迹并在进程内执行最小反例，未修改活跃源码、停止运行或合并。

## 当前进度

- main 仍为 1dfde3c，Wiki 分支尚未提交合并。主干五轮 train conv-26 / validation conv-47 / test conv-49 验收尚未开始。
- wiki_gap_repair_20261005_c26：B0 10/10，R1 9/10、R2 9/10、R3 10/10，全部拒绝，正式故障0，summary complete。证明运行健康和拒绝退化，不证明优化带来收益（基线满分天花板）。
- wiki_gap_repair_20261005_mc：进程仍存在；冒烟已过，当前 B0 conv-30 的题0出现 f_filter_facts 的30001/30000步资源故障，正式阶段尚未全部完成。
- Travel loop3：B0评分 completed0/1，evaluation_faults1（缺gradio），R1/R2评分完成、生成故障0，但各0分；整体summary failed。README将其称为完整健康B0闭环不准确。

## P1：准入正例混淆结构合法与语义正确，驱动删除有效C检查

oak/kernel/checks.py synthetic_answer_variants 将契约最小实例当成必过正例：城市字段为空；多元素实例为复制同一个 days=1 的对象，得到 days=[1,1]。admission对这些候选的任一C拒绝均判准入失败。

Travel R1 attempt-2..5 正确拒绝空城市/重复日期，却一直被挡。attempt-6删除城市非空、日期序列和字段检查后通过。最小复现：同一份 days=[1,1] 的候选，attempt-3 C拒绝（days sequence 1,1 is not 1..N），通过准入的R1 C放行。见counterexamples.json。

修法：结构电池验证冻结类型/可执行性，不能要求语义C认可契约最小占位实例；语义must_pass必须来自与参数/证据一致的已验证真实候选。增加重复日期、空城市等任务适配层负例，避免把合法语义拒绝当作实现bug。不要用删除C逻辑来换准入通过。

## P1：正式/准出归因因35000字符门而缺失，循环记忆链没有完整发挥

loop3 optimization/maintenance 的 failure 文件明确记录：B0 formal、R1 decision、R2 decision均 Maintenance evidence exceeds 35000 characters。state limit10/reserved_calls4，非额度耗尽。R1另有一次服务空返回。

两轮 goal.json 均 evidence_ids=[]、泛化默认direction；原始事实仍可读，但不能称正式结果的归因指导了下一轮优化。

修法：在维护器请求构造前对当前单条训练例的轨迹/候选/原文和资产代码做字段级预算压缩；保留检查ID、参数、失败签名、类型事实及原始证据引用。确定性的超长失败允许压缩后再尝试；不重放未知是否消耗的模型请求，不简单扩大上限。补单训练例超长和准出归因可落盘的反例。

## P1：多对话新候选F大limit调用仍会耗尽预算，准入没有覆盖

conv-30题0：f_filter_facts(subject=乔恩,fact_type='',date_prefix='',limit=500)，steps_used30001/30000，nodes调用1次。F逐行归一化10个字段，返回行数大时耗尽；不是CPU死锁，执行观察约36.6ms。

B0 admission中同工具的base/stress/wide_filter参数全部limit20，未测试真实500调用。

修法：对可放大读/返行数的数值参数增加合法大值与宽过滤的组合试跑；候选F要在昂贵逐行操作前依据预算限行或采用批量算子，并准确返回truncated/省略信息。不要仅扩大function_steps/timeout掩盖问题。

## P2：verified_fix仍未强制绑定具体回放

oak/experiments/wiki.py _lessons 在同资产通过准入时无条件设admission_verified和verified_fix；reproduced_checks只是有proof时追加。没有proof仍会成为已验证修复，且proof未匹配到旧失败具体snapshot/signature。

counterexamples.json复现：旧C失败＋同C新准入通过、scenarios空列表，仍生成admission_verified/verified_fix。

修法：资产过门与特定故障修复分开；只有case/候选快照digest/check_id/失败签名绑定的复现和对应负例检查通过，才能设置该经验的verified_fix。保留F实际回放的验证路径，不用C专属证明替代全部F验证。

## P2：Travel验收口径与运行配置

B0评分失败是基础环境问题，不属于任务难度；现有loop3整体failed不能说完整健康。环境修复后应新目录验证B0及后续轮次，不修改旧评分。

travel_loop1.py使用function_timeout_s=15（原默认2）、protocol_attempts5；这些属于配置变更，应明确列为预算调整，不能一并声称没有放宽。主干验收按约定预算或明确隔离对比。

## 建议顺序

先修正例语义与超长归因，再补大数值压力/verified标签；保持活跃运行证据。三套回归及小规模真实循环通过后再合并。合并后按用户新增要求完成主干三个对话划分、至少5个正式训练轮，验证/测试不回流Wiki或提案。
