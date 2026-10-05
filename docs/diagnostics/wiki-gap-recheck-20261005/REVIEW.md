# Wiki 修复二次复查（2026-10-05）

结论：修复有进展，但仍有三个 P1 和一个 P2，不能把本轮标成“全部修复 / 可以合并”。

本次只审查 Wiki 工作树、回放归档输入、运行离线反例和回归测试。没有修改活跃源码、冻结数据、旧评分或运行状态，没有停止运行、合并或提交，也没有新发真实模型请求或运行全量题目。新增文件仅在本目录。

## 独立验证结果

- 框架：`python -m unittest discover -s tests -q`，307 项，OK，跳过 2 项。
- LoCoMo：14 项，OK。Travel：11 项，OK。
- 本目录 `recheck.py` 用当前代码、临时目录、RecordedClient 回放旧 Travel 的 5 条 formal/decision 事件；4 条可以写归因，原 B0 formal 仍因 35000 字符限制退出，模型调用为 0。
- 正常 C、冻结容器错误 C、拒绝所有 answered 的 C 均走当前真实准入逻辑：三个都通过；同一归档正常候选走 KernelRuntime.check_candidate，正常 C 接受，两个坏 C 拒绝。
- 在 conv-30 冻结真图、原 F、30000 步 / 2 秒 / 180KB 下，当前 `_samples` 生成的 6 个压力样本全过，历史真实调用仍在 30001 步失败。
- Wiki lesson 的两个独立反例仍得到 admission_verified：场景同名但 input_ref/参数不同；结构场景 status=passed 但 C.ok=False、原“answer is not an array”仍存在。
- 反例运行前后 5 个相关源码/任务文件哈希一致。完整结果在 `counterexamples.json`。

## 必须继续修复的四项

| 优先级 | 问题与实证 | 修复方案与验收 |
|---|---|---|
| P1 | C 准入现在允许正常答案全部被拒。`admission.py:351` 将 structure 行的任何良构拒绝算通过；结构化任务唯一 must_pass 是弃答。“只接受弃答、拒绝所有 answered”的 C 通过全部电池与任务反例，冻结数组误判 C 也通过。 | 保留占位实例的结构档，避免重新要求空城市占位实例语义必过；同时由任务适配层提供可确定验证、与问题/参数/证据匹配的正常 answered 正例，自动进入 must_pass。历史 verified 回放目前只有显式 promotion 才成为必过，不能充当自动覆盖已经完成的证据。正常 C 必须过；全拒 C、容器误判 C 必须在正式答题前被拦；重复天序/空城市反例仍须拒。禁止利用评测标准答案构造推理输入。 |
| P1 | 单例超长压缩仍未覆盖真实字段。`wiki.py:183` 压 answer，但真实字段为 generated_answer；rows/candidate_summary 位于 training_examples[].trace[]，而 `:188` 只检查 facts 顶层 trace。原 B0 压缩后 facts 本身仍 36247 字符，其中嵌套 trace 18993 字符；加运行契约后仍被拒。4 条 R 事件通过只能证明部分修复。 | 按生产字段逐层压缩 generated_answer、trace[].rows、trace[].candidate_summary.answer、长 summary 和资产内容，保留失败事件及前置调用、检查ID/issues/json_type/参数/步数/来源指针。原始事实完整持久化，请求摘要按完整 payload 的总预算逐级收紧。用归档 B0/R1/R2 formal/decision 全部作为回归，不能仅测试自造 answer/rows 字段；5 条都应形成不超过 35000 字符的请求并写归因。 |
| P1 | 数值变体没有与宽过滤组合。`runner.py:260` 只从原 base 放大 limit；得到“乔恩＋事件＋500”和“吉娜＋2023-05＋500”，都能过；清空过滤条件的样本仍 limit20。原实际“乔恩＋空类型＋空日期＋500”耗尽 30000 步，新增样本仍漏掉原故障。 | 对可合法放大的数值参数，在原 base 与宽过滤/代表性真实字段变体上组合压力测试，再经契约验证与去重；保留历史失败实参回放。原故障 F 应被准入拦截；修复 F 应在相同预算下通过，正确披露截断，不能靠放宽预算。测试必须执行原 F＋真图＋实际调用，不能只断言样本里存在 limit>=500。 |
| P2 | verified_fix 仍只绑定资产ID和场景名字。`wiki.py:130` 不比较 input_ref/参数/快照/失败签名；结构行 ok=False 也可被当作修复证明。空 scenarios 漏洞已堵，但“改了输入”及“旧误拒仍存在”两类仍误标。 | 记录并绑定原 case、参数/快照 digest、检查/工具ID、旧指纹和故障签名。区分“检查可执行”“符合该样本预期”“原故障消失”：structure 通过不能证明正常候选误拒已修复；must_reject 的通过则需要非法候选被拒；F 需要原实参在对应图上成功。满足原失败的对应期望后才给该条经验 verified_fix，否则保持 unresolved/hypothesis。补这两个反例，不用同名场景代表同一次复现。 |

## 哪些修复已有证据

- 空城市/重复天序的合成正例不会再迫使正常语义 C 删除检查；正常 C 可以通过结构档，同时任务层的两个负例拒绝删掉语义检查的 C。多元素 days 变为 [1,2]。但新增全拒漏网问题仍需解决。
- 归因压缩已有部分作用：R1/R2 formal/decision 四条旧事件现在均能写归因。B0 仍未覆盖。
- Travel 官方评分环境修复有真实 B0 证据：loop4 B0 completed1/1，generation_faults0、evaluation_faults0；正式归因已写入。final/macro_cs/macro_hc 均 0，不能称有任务收益。
- Travel loop4 function_steps/timeout/result_bytes 为默认 30000/2s/180KB，protocol_attempts5 显式声明。旧 loop3 的失败记录没有被改写。

## 当前真实运行的验收边界

采样时刻和完整状态在 `run_snapshot.json`。

- 多对话旧目录 `wiki_gap_repair_20261005_mc/train`：B0 completed4/6、generation_faults2，R1 尚未形成正式结果。当前源码与 experiment.json 的 6 个冻结文件指纹不同；直接调用 assert_files 可复现 `Frozen engineering/input files changed`。这是修复前启动的运行，不能用来证明当前版本通过，不能据此声称当前修复又产生两次正式故障。保留旧证据，源码固定后以新目录/新身份验证当前版本，不覆盖冻结哈希来续用旧身份。
- Travel 新目录 loop4：源码冻结指纹一致，B0 健康、得分0，R1 进行中；尚未完成两轮正式循环，不能宣告 Wiki 收益验收完成。
- main 仍为 1dfde3c。合并后的 conv-26训练 / conv-47验证 / conv-49测试、至少5个正式训练轮的主干验收尚未执行。

## 下一步

先一次补齐上述四项，把本目录反例变为有意义的红→绿回归。固定源码后在新目录做小规模真实多对话和 Travel 循环，核对准入/正式/准出归因与下一轮 evidence_ids。旧记录不能代替新版本验证。通过后再交由用户指定 agent 合并，并在主干执行既定三组至少5轮验证；验证/测试集不回流 Wiki 或提案。
