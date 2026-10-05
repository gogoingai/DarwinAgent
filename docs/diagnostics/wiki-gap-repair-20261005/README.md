# Wiki 两缺口修复验证（2026-10-05 phase 2，含四轮审查修复）

上游：`docs/WIKI-VALIDATION-RESULTS-20261005.md` §5 两条建议（动态图真实数据准入、
C 失败候选可验证经验回放）。本轮在 Wiki 工作树完成修复并做小规模真实模型验证；
全部新运行用新目录新身份，旧运行一律不动。
**四轮独立审查**（`wiki-gap-review-20261005/`、`wiki-gap-recheck-20261005/`、
`wiki-gap-recheck3-20261005/`、`wiki-gap-recheck4-20261005/`）累计发现 10 项，全部
修复并补红→绿回归（回归 333+14+11 全绿）。

## 四次复查修复（recheck4，含真实反例复现）

- **P1 Travel 正例误挡合理 C**（`remaining-probes.json` 复现）：旧
  `two_day_consistent` 夹具 day1 三餐全空、Springfield 在官方城市集/餐厅库 0 命中
  （day2 实体全部虚构）、餐食非官方形态，且证据硬取活案例图首 1–3 行——执行官方
  「非移动日三餐」语义的合理 C 被 must_pass 行误拒。修复：重造**真实城市对往返
  夹具** `two_day_round_trip`（St. Petersburg↔Rockford 两日闭环——官方约束要求
  行程首末城市一致；航班 F3573659/F3573120、六餐厅、两 minimum-nights=1 住宿、
  两景点全部取自官方数据库；餐食/景点官方 `Name, City` 形态；总成本 $1,464<
  $2,000），夹具自带 12 行独立证据行整集进快照（`counterexample_snapshot` 优先
  用夹具 evidence_rows），**官方 commonsense+hard 校验通过并固化为防漂移测试**。
  红→绿：接地语义 C（三餐非空＋城市一致＋实体在证据中存在＋航班在证据中）接受
  新夹具（旧态红）；旧 Springfield 形态与虚构航班/餐厅仍被拒；全拒 C/容器误判 C
  仍被 answer_ex 行拦。
- **P1 Wiki 跨对话误判旧故障已修复**：`_verifies` 只做参数 digest 子串匹配——
  base/stress 参数来自资产级 trial_inputs，跨 case 必然同 digest，旧 case 失败可被
  新 case 通过行「验证」。修复为五要素绑定：**case 前缀＋场景族＋参数/快照
  digest＋数据图身份＋期望档≠structure**（origin 增记 graph_digests，图变则旧
  验证不可沿用——为可重建图模式铺路）；证据不足只记候选通过，lesson 保持
  unresolved。红→绿：跨 case 同参数（remaining-probes 反例）、同 case 跨场景
  （stress 失败＋base 通过）、图 digest 不一致三态全部不再验证（旧态全红）；
  同 case 同场景同 digest＋同图仍验证（不回退）。

## 新模式：冻结记忆/向量、图可重建（recheck4 快速循环入口，2026-10-06）

按 recheck4「新实验的最小方案」实现，旧模式行为零改动：

- **装配**：`Pipeline(graph_builder=...)` 新分支——记忆/向量仍取快照（digest 校验），
  图由任务侧 builder（`datasets/locomo/graph_rules.rebuild_snapshot_graph`）按当前 S
  从固定事实确定性重建（零模型调用），挂冻结向量索引＋命中映射校验（任一事实行无
  向量记录＝硬失败）；质量门与冻结路径同构（F 试跑＋反例探针＋任务图 C）；运行身份
  拆分（graph_mode/builder 源码摘要进身份，rebuild 与 frozen 不共享检查点）。
- **事实→图投影**（graph_rules.py）：固定事实行→`build_graph` 候选；行词表与冻结图
  一致（原子事实键=编号，向量命中映射两侧同一索引）；S 即构图规则——声明过滤（S
  少声明则图真实变小），原子事实/编号 主键为硬前提（违反即拒，不静默降级）；
  mentions 无类型信息，不发涉及*边（能力边界如实报告）。真实 bootstrap S 实测：
  971 事实行全量、向量命中 971/971、主题/会话对齐。
- **轮预算**：`proposal_attempts`（新模式可配，旧模式默认 50 不变）＋
  `round_deadline_s`（round 起固定、不随 attempt 重置；到点持久化 state=deadline、
  决策记 validation_failed、不计正式轮）；冒烟前再查一次。
- **验证进循环**：`--validation-question-ids` 同对话固定 10 题；B0 建验证基线，
  训练主判通过的候选对同 10 题评分，聚合指标进决策与 Wiki（primary 严格升＋floor
  不降＋无故障，退化即拒）；逐题 gold/答案/诊断不进提案器；validation.json 逐轮落盘。
- **P.extract 惰性**：此模式不执行 P.extract；候选补丁触及 P.extract 时决策与 Wiki
  formal 记 `p_extract_not_effective`，不得报告为已生效优化。
- **划分**：`datasets/locomo/scripts/question_split.py`（conv-26 固定种子、题型分层、
  同证据事实同组；15 训练＝[2,3,6,7,31,57,85,114,139,145,147,164,172,173,174]，
  10 验证＝[18,44,55,74,118,128,133,148,163,179]，确定性已验证）。
- **准入面＝作答面**：候选准入电池在「按当前 S 重建的图」上跑（runner
  `_rebuild_graph_cached`，键＝事实摘要＋S 指纹＋builder 源码；F/C/P 改动复用、
  S 变重建）；B0 bootstrap 试跑同构（trial_supply）。
- **离线测试**：test_fastloop_mode（预算退出/提案上限/验证选版双向/P.extract
  标注/缓存键）＋ test_locomo_graph_rules（投影确定性/S 塑形/硬前提/向量命中/
  缓存键）共 12 项；三套回归全绿。
- **真实短验 ✅（`wiki_fastloop_short_20261006`，2026-10-06）**：summary **complete**
  无 unhealthy——B0 过重建图引导试跑＋进程内准入（73 行电池，graph_digests/
  snapshot_digests 绑定）＋冒烟，训练 11/15 零故障（85.7s）；**B0-val 验证基线
  建立**（6/10 零故障，validation.json 落盘）；R1 首提案即过准入与冒烟（≤3 提案
  预算内），正式 9/15 降分被正确拒绝（metric_decreased×2），**整轮 186.9 秒**
  （900 秒硬顶内，round_elapsed_s 入决策）；答题图＝重建图（1026 节点＝971 事实
  ＋6 人物＋29 主题＋20 会话，2968 边，diag 带 projection_version 与冻结
  facts/vector digest）——非快照冻结图。短验过程连环暴露并修复五个接缝缺陷
  （bootstrap 试验支路优先级、preflight 重建失败报告化、包装图兼容、投影词汇
  样本一致性、corpus 形态契约），全部带离线回归测试。

## 三次复查修复（recheck3，全部带反例→红绿回归）

- **P1 旧模型输出不能当语义正例**：删掉 legal_plan（官方评分 final=0、语义不合法
  却被 must_pass 强制接受）；`answer_examples` 改为任务自建自洽夹具
  （two_day_consistent：自带问题/参数/候选，首日=org、末日=dest、天序 1..N、
  非转移日三餐非空，确定性自检）。快照构造优先用夹具自带问题/参数——换 case/
  图不会把旧行程错装。验收：参数一致＋三餐规则的语义 C 过电池；同 C 对
  「旧行程＋不匹配参数」判拒。
- **P2 输入绑定只对三前缀生效**：改为全场景「原输入 digest 成功才算修复」——
  lesson origin 的 input_ref 尾段（≥12 hex＝参数/快照 digest）在通过行的 ref 中
  命中才验证；A→B 失败仅证 A→C 成功不再误标。无 digest 的静态/固定负例按场景
  身份；structure 档永不作为语义修复证明。
- **P1 摘要丢故障事实**：`_brief_facts` 场景行保留 issues/ok/expectation/
  error_type/step_budget/check_id/json_type；Tier3 骨架保留 candidate 事件的
  checks[].{check_id,ok,issues,steps_used}、candidate_summary.json_type、
  observation.{steps_used,step_budget}。验收：loop6 真实 R1 拒绝理由
  （'structured_answer is missing or not parseable JSON'）在维护请求中可读；
  1200 字符级预算下嵌套签名/类型/步数保留；旧五条归档归因继续通过。

## 二次复查修复（recheck，全部带反例→红绿回归）

- **P1 全拒 C 漏网**：任务适配层新增 `answer_examples`（task.yaml 声明真实合法
  计划＝归档 evalfix B0 模型产出，非金标）→ 准入 `answer_ex_*` must_pass 行；
  「只接受弃答、拒绝所有 answered」与冻结容器误判 C 均在正式答题前被拦。
- **P1 压缩未覆盖生产字段**：`_compress_training_evidence` 重写为真实字段形态
  （generated_answer、trace[].rows/candidate_summary/summary/feedback），按
  「完整 payload −其余部分」总预算三级收紧；归档 loop3 全部 5 条 formal/decision
  事件离线回放全部形成 ≤35000 请求并写归因（含原 B0 formal）。
- **P1 数值未与宽过滤组合**：字符串清空子集（≤16）×数值放大（max(500,v×25)）
  组合变体＋AST 种子成员校验门；conv-30 原 F（limit=500 预算耗尽）在真图准入
  被拦（真实失败实参回放），修复 F 同预算通过并披露截断。
- **P2 verified_fix 绑定弱**：lesson 记录 origin（input_ref/签名）；验证要求
  同资产＋同场景＋（replay/check_replay/answer_ex 类）同 input_ref；结构档
  （expectation=structure）通过永不作为语义修复证明。同名不同输入、结构档
  ok=False 两反例不再误标。

## 审查修复（全部带最小反例→红绿测试）

- **P1 正例混淆结构合法与语义正确**：契约最小占位实例不再作为语义必过正例——
  电池分三档（must_pass=真实行/拒答形态；structure=占位实例只验证冻结容器下
  可执行＋意见良构，语义拒绝合法；must_reject=畸形＋任务反例）。多元素实例在
  items 层取 seed（days=[1,2]，不再复制出 [1,1]）。任务适配层新增
  `answer_counterexamples`（task.yaml 声明重复天序/空城市）拦截「删语义检查换过门」
  的 attempt-6 形态。容器误判缺陷的兜底移交缺口②的真实候选验证回放
  （`test_container_bug_c_passes_battery_but_verified_replay_blocks`）。
- **P1 维护归因 35000 超长即放弃**：新增 `_compress_training_evidence` 字段级
  渐进压缩（保检查 ID/参数/失败签名/类型事实/步数/证据指针计数，压长文本/行集/
  原文块）——请求发出前确定性压缩，不重放模型请求、不扩大上限。
- **P1 大 limit 预算耗尽无准入覆盖**（conv-30 f_filter_facts limit=500 →
  30001/30000）：压力样本新增合法大数值变体（数值参数→max(500, v×25)），
  不放大 function_steps/timeout。
- **P2 verified_fix 未强制绑定**：lesson 标 admission_verified 现要求「最初失败
  场景在本次准入有 required+passed 行」（loop_carried 等静态规则通过时同样落行，
  场景行集完整）；空 scenarios 反例不再产生已验证修复。
- **P2 口径与预算声明**：loop3 整体 summary failed（B0 评测环境故障）不得称
  「完整健康 B0 闭环」——R1/R2 链条健康、B0 环境修复后由 loop4 新目录补验；
  Travel 预算声明：steps/timeout/result_bytes 用框架默认（30000/2.0s/180KB），
  protocol_attempts=5 为显式重试预算调整（对齐 LoCoMo arm_config 参照）。

## 修复内容（先复现后修复，红→绿）

缺口①动态图真实数据准入：
- `runner._preflight` 第三分支：`dynamic_trial=True`（任务装配层注入，核心零任务硬编码）
  时动态图任务进进程内 `admit_candidate`，per-case 真图供给；
- 图供给 `_dynamic_trial_graphs`：候选补丁未触碰 S/P.extract → 复用已采纳 stage 同题
  真图（stage.asset_version==候选基线版本＋graph.complete.json digest 校验）；触碰 →
  `ExtractionAgent` 候选资产重抽，键控缓存（case+S+P.extract+config+transport）跨
  attempt/resume 复用；
- B0 门/bootstrap 试跑环（`trial_supply`，ModelSession 支持 async 校验器）同构开启；
- 冒烟门对动态模式开启：单题 case=执行故障门＋有效作答＋（有判题器时）判题完整；
  Travel 不移植 ≥2 质量门（单题无从定义；LoCoMo 6题/≥2 门原样）；
- `synthetic_answer_variants/snapshot` 按 `spec.answer_contract` 合成最小合法实例
  （string 契约路径逐字节不变）；`stress_trial_samples` 同类实参互换热样本
  （枚举型契约的空串变体非法导致压力覆盖空转的修复）；`function_check` 喂契约实例。

缺口②C 失败候选回放：
- `answer.py` 候选检查失败事件原位留存完整检查快照（visible_evidence 行数封顶、
  整体 200KB 上限、旧检查点无字段可继续读）；
- `_prior_failed_check_snapshots`（镜像 F 参数回放库，不重建 F 库）扫全历史阶段：
  机器可判定分类 must_reject（空/不可解析/违反 answer_contract——任何 C 必须拒）、
  verified_must_pass（经具体复现验证后由 `promote_verified_check_replay` 持久化，
  成为强制回归行）、informational（类型合法被拒——语义拒绝不自动判 bug，不设门）；
- `admit_candidate(replay_checks=...)` 三族场景 `check_replay_{reject,verified,info}`，
  worker 载荷透传；归因假设不覆盖回放事实（回放行输入/判定/issue 原样进报告）；
- `wiki._lessons` verified_fix 绑定 `reproduced_checks`（本资产参与的 check_replay
  通过行）——同一资产过准入≠历史问题修复。

已知故障复现（tests/integration/test_wiki_faults_repro.py，夹具=归档证据逐字拷贝）：
T1 F 空值契约（空串压力变体逐字复现 `tool.result.inbound: expected object`，准入拦截）、
T2/T3 冻结容器误判（契约电池定位到 day 级签名）、T4 非法输入任何版本必拒、
T5 沙箱白名单（hasattr）。另如实暴露：归档 attempt-4「修复版」C 对弃答形态仍误杀
（四标签回放未覆盖 abstain）。

## 本目录脚本

- `travel_loop1.py <运行目录名> [--resume]`：缺口修复后真实 Travel Wiki 闭环驱动
  （train:0，rounds=2，dynamic_trial=True）。日志 `travel_loop*.log`。

## Travel 真实闭环结果（wiki_gap_repair_20261005_loop1/2/3，全部保留）

- **loop1（失败证据，保留）**：首版草案真图抽取一次协议失败（引文非逐字子串＋
  JSON 手误）即终止冷启动——暴露框架缺口：试验图构建失败应转校验反馈。
  修复：bootstrap 试用块把抽取异常包成 ValueError 反馈（`bootstrap.py`）。
- **loop2（B0 被冒烟门拦截，保留）**：动态准入首战全绿（B0 admission：25 场景
  全过、graph_digests 绑定、试验图缓存 1 条、压力样本 base×5+stress×10+
  wide_filter×5）；冒烟门正确拦下系统性契约违规（模型答案 `lunch:null` 违反
  string 契约，重试两次未改）→ blocked_b0。修复（协议层，非门放宽）：
  bootstrap 规则补 json 契约未选值归一化（string→空串）；Travel 重试预算对齐
  LoCoMo 参照（protocol_attempts 2→5）。
- **loop3（链条健康，口径更正）**：B0 准入✓冒烟✓；B0 正式生成零故障、评测故障 1
  （环境：官方 utils/func.py 顶层 import gradio，venv 未装；仅 UI 校验用，评测路径
  不需要——`_worker.py` 注入最小 stub 于轮间边界修复，B0 故障如实保留）；R1 提案
  经 7 次尝试后 attempt-6 通过准入（注意：其中 attempt-2..5 的拦截含旧电池的
  「占位实例当语义正例」误拦——审查 P1 已修复该口径）；R1/R2 正式评分 complete
  （1/1，生成/评测零故障）→ 决策回写拒绝（final 0 vs 0，正确）。wiki 16 条全生命
  周期。**整体 summary failed（B0 评测环境故障）——不能称完整健康 B0 闭环；
  干净 B0 闭环由 loop4（审查修复后代码＋干净评测环境＋声明预算）补验。**
  运行中 30 分钟后台时限被杀一次，同身份 `--resume` 从 R1 候选恢复（复验准入
  快照→冒烟→正式），未篡改旧身份。
- **loop4（B0 干净证据；R1 中止）**：最终代码下 B0 complete（148s，准入✓冒烟✓
  正式评分零故障——gradio 环境修复生效），得分 0/1（如实：无任务收益）。R1 在
  50 次上限内震荡（旧电池下弃答误拦↔新电池下装饰性 C 误拦），二次复查修复落地
  前主动中止，未完成两轮——不能作验收运行；其 B0 证据保留。
- **loop5（B0 被冒烟门拦截，保留）**：二次复查修复代码下 B0 冒烟全灭于改名/日期
  偏移反例探针（cold_bootstrap 草案 F 误伤）——该探针对冷启动 bundle 在冻结快照
  路径本有豁免（提案轮恢复全量），动态图路径漏了同等豁免；已对齐（pipeline.py），
  loop6 起新根重试。
- **loop6（最终代码判定运行，✅ 完整健康闭环达成）**：summary **complete**、
  unhealthy 空——B0 complete（1/1，生成/评测零故障，干净环境）；R1/R2 均
  complete（1/1 零故障）→ 决策回写（final 0 vs 0，`primary_not_strictly_improved`
  正确拒绝）；wiki 31 entries；**R1/R2 goal.json evidence_ids 非空、direction 来自
  真实归因文本**（超长归因修复在真实循环生效——对比 loop3 全部退化）。
  预算：steps/timeout/result_bytes 框架默认，protocol_attempts=5 显式声明。
- **loop7（三次复查后最终代码判定运行，✅ 完整健康闭环复现）**：summary
  **complete**、unhealthy 空、未人为中止——B0 complete（0/1，生成/评测零故障，
  141s）；R1 五次准入尝试后 attempt-5 通过，正式 0/1 零故障，决策正确拒绝
  （`primary_not_strictly_improved`）；R2 0/1 零故障，同样正确拒绝；wiki 20
  entries / 20 events；R1/R2 goal evidence_ids 非空、direction 来自真实归因；
  试验图缓存 1 条（复用支生效）。与 loop6 同判：**Travel 健康闭环在最终代码上
  复现**（loop6 为二次复查代码下的首次达成）。
- 结论：**Travel 健康闭环在最终代码上达成并复现（loop6、loop7）**；loop1–5
  全部保留为归因与修复链证据。

## 验证矩阵（不跑全量、不用 conv-47/49、金标不进优化侧）

1. ✅ Travel train:0 ≥1 次健康 B0→提案→准入→正式评分→决策回写（loop6 首次
   达成、loop7 最终代码复现；loop3 R1/R2 为链条健康早期证据）；
2. conv-26 固定 10 题两轮真实验证：
   - 首轮（`wiki_gap_repair_20261005_c26`，二次复查前代码）：summary complete、
     unhealthy 空——B0 10/10、R1/R2 9/10 拒、R3 10/10 平分拒，全程零故障，
     wiki 18 entries。
   - 复验（`wiki_gap_repair_20261005_c26b`，三次复查前代码）：summary complete、
     unhealthy 空——B0 8/10，R1 8/10 平分拒，**R2 9/10 采纳**
     （`primary_strictly_improved`），R3 9/9 平分拒；零故障；wiki 73 entries。
   - **三次复查代码复验（`wiki_gap_repair_20261005_c26c`，2026-10-05）**：summary
     complete、unhealthy 空——B0 9/10，R1/R2 9/10 平分拒，R3 8/10 降分拒
     （`metric_decreased`×3 正确）；全程零生成/评测故障；三轮 goal evidence_ids
     全非空；wiki 18 entries。
3. ⏳ conv-30/41/42 各 2 题（0,20）：
   - 首跑 mc（旧电池代码）B0 failed 4/6（conv-30 题0 limit=500 步数耗尽，审查
     P1-3 实锤证据保留）；R1 因源码修改触发冻结守卫中止（守卫按设计工作）。
   - mc2（二次复查代码）B0 引导阶段因三次复查修复落地主动中止。
   - **mc3（三次复查代码，2026-10-05）**：B0 如实失败（5/6，conv-30 题20 确定性
     生成故障 `Answered results require evidence and no error`，归因完整）；R1
     正确拒（清零故障但主分未升）；**R2 采纳**（21 次准入尝试后
     `primary_strictly_improved` 4/6→5/6 且故障清零）——第二个真实经验复用改进
     案例；wiki 41 entries。R2 的 21 次尝试跨约 80 分钟＝recheck4「提案重试治理」
     （新模式 ≤3 提案/15 分钟预算）的直接论据。
   - **mc4（四次复查代码）补验中**（与 loop8/c26d 同批，源码固定后新目录）。
4. ✅ 回归三套全绿（333+14+11，新增 14 项）；既有十轮证据审计完成（旧运行
   零回放行，新场景无历史行时不产生新拦截）。
5. **四次复查代码重验波（2026-10-06）**：
   - **loop8 ✅ 完整健康闭环**（三连：loop6/7/8）——summary complete、unhealthy 空，
     B0/R1/R2 全 complete 零生成/评测故障（0/1 如实无任务收益），决策正确拒绝，
     wiki 13 entries，R1/R2 goal evidence 非空。新夹具（two_day_round_trip）的
     answer_ex 行在真实准入电池中运行。
   - **mc4 ✅ complete、unhealthy 空**——B0 5/6 **零故障**（mc3 的 conv-30 题20
     确定性故障本次冷启动未复现——不同草案抽样），R1/R2 候选 4/6 降分正确拒绝。
     R1 attempt-1 曾被我方一次短暂的 wiki.py 编辑/回滚触发冻结守卫拦截（工程侧
     改动，wiki 归因如实记录为环境事件非候选缺陷）；文件即时恢复字节一致，后续
     attempt 自愈继续。wiki 13 entries。
   - **c26d ⏳**：B0 complete 10/10 零故障；R1 准入尝试进行中。
