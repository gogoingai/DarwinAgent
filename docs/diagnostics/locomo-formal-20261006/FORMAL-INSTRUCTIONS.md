# LoCoMo 正式迭代执行指令

准备日期：2026-10-06。本文件只准备协议和命令，尚未启动模型任务。开始时间由操作者决定；开始前先结束、归档当前外测，不与它争用模型额度。

## 固定协议

| 项目 | 本次执行要求 |
|---|---|
| 训练/验证 | 只用 conv-26，在全部 199 题中固定划分 120 题训练、79 题验证；使用本目录 split.json，不再用旧 15/10 划分 |
| 划分方法 | 五类题按比例覆盖；共享标注证据的完整连通组不跨侧。已核对题号无重复、两侧覆盖全部 199 题、标注证据无交集。这不能保证不存在语义关联 |
| 记忆完全冻结 | 准备阶段先补齐缺失的 conv-44：优先复用现成事实/向量，找不到则重新提取事实并构建向量，完成校验后冻结。已有 9 个对话不重新提取或覆盖。进入本次迭代/对比后，facts.jsonl 的事实、编号、出处、日期及字段和 vector/index.jsonl 全部冻结，不纠错、补写、删除或重做索引。查询嵌入及其缓存允许按正常检索产生 |
| B0 冷启动 | 新目录、新 S/F/C/P 资产、空 Wiki；不载入旧 R6、旧候选、旧 Wiki、旧提案记忆或旧运行图。B0 图按新 S 从冻结 facts 确定性重建 |
| 后续图 | 开启 graph-rebuild；候选 S 改变时重建，F/C/P 改变且构图输入不变时可复用同身份图。准入、冒烟、训练、验证及最终测试使用同一构图规则 |
| fast 模型 | 正式启动前将 fast 层切换到项目现有 DeepSeek 配置，完成预检后锁定实际模型/路由。迭代和最终两臂对比共用此配置 |
| 优化范围 | 开放有效 S/F/C/P 联合修改，不限每轮只改一个类型。冻结记忆模式下 P.extract 不执行，其变化不能记作收益。当前构图器只投影已有事实字段和人物/主题/会话关系；新 S 声明不会自动生成构图器未实现的关系 |
| 轮数和预算 | B0 + R1–R10；每轮最多 3 次提案，整轮上限 7200 秒，包含提案、准入、冒烟、训练、验证、Wiki 和发布。该值是保护上限，不是目标耗时。B0 不受此参数约束，单独监控 |
| 采纳 | 保持现有双门槛：训练 repaired_precise 严格提升，其他训练指标不下降；随后验证 original_precise 严格高于最近采纳版本，original_lenient 不下降，79 题评分完整且生成/评测故障为零。验证未提升或持平不采纳。训练不过门槛时当前框架不跑该候选验证，须明确记为未验证 |
| 信息边界 | 提案只读本次 Wiki；训练轨迹可用于经验归因。验证只回流聚合指标/采纳理由，不向提案器或 Wiki 写验证逐题 gold、参考答案、错题分析或轨迹。最终测试不回流优化 |
| 目标 | 验证集有真实提升；锁版后在其他对话上相对纯向量有可重复审计的收益。目标不是强行采纳：不满足就保留 B0 并如实报告 |

120 题训练的类别数：1/2/3/4/5 = 19/22/8/42/29；79 题验证为 13/15/5/28/18。轮数、题号、预算、模型和代码身份须在首次启动前锁定，不能在同一运行目录中改参数扩轮。

## 启动前处理

1. 等当前三对话外测结束，保留完整报告和未解决故障清单；不重跑已经成功的原测试。
2. 核对主干实际代码及工作区。当前 graph_rules.py、external_test.py 有其他 agent 的修改，须等修改和验证完成后固定正式代码，不覆盖、不在运行中继续编辑。
3. 先将 fast 层切换到项目现有 DeepSeek 配置并记录实际模型/路由，再固定模型路由、温度、作答/审查预算、判题器、原始/修订 gold、语料和全套相关代码，按切换后的配置重新预检；额外记录整个 graph_rules.py 的摘要，不能只依赖 builder 函数体的摘要。新实验单独记录 commit 和工作区差异。
4. 先按下述流程补齐 conv-44，再为全部 10 个快照的 facts.jsonl、vector/index.jsonl、manifest.json 记录摘要；已有 9 个快照禁止重新导入覆盖。正式阶段结束后再次核对。确认新 B0 的 graph.json 由新 S 重建，原子事实编号和固定向量能一一映射；不能只看到命令有 graph-rebuild 就算核对完成。
5. 保留已完成的稳定性检查证据，不另开十轮小循环。新 B0/候选仍须经过框架真实图准入及冒烟；发现确定性代码故障先保存现场、暂停和修复，不能靠无效轮凑满十轮。

本次最终对比必须覆盖全部 10 个对话、1986 题。目前已有 9 个对话的冻结快照，缺 conv-44 的 158 题。准备阶段先查是否有可复用的 conv-44 事实和对应向量；找到则导入并校验。找不到就按现有记忆提取流水线，从 conv-44 对话原文重新提取事实、构建向量及所需快照，使用既定提取/嵌入配置并记录实际配置；不读取 QA gold 或参考答案生成记忆。校验出处、事实编号、向量映射后冻结，V0/G1 共用这同一份数据。既有 9 个对话的事实和向量保持原样。补齐后更新本次数据清单及摘要，再进入正式迭代和全题对比；不能把缺失 conv-44 的 9 对话结果作为本次最终完成结果。

## 正式启动命令

以下仅在上述条件完成、操作者选定凌晨启动时间后执行。准备阶段没有创建自动定时任务。

```bash
cd /Users/xu/git/oak
export PYTHONPATH=.

# 只在第一次启动时生成一次根目录；记录下来，恢复时必须用同一个值。
OAK_FORMAL_ROOT="datasets/locomo/runs/locomo_formal_frozen_memory_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OAK_FORMAL_ROOT"
cp docs/diagnostics/locomo-formal-20261006/split.json "$OAK_FORMAL_ROOT/split.json"
printf '%s\n' "$OAK_FORMAL_ROOT" > "$OAK_FORMAL_ROOT/RUN-ROOT.txt"

OAK_TRAIN_IDS=$(.venv/bin/python -c 'import json,sys; print(",".join(map(str,json.load(open(sys.argv[1]))["train"])))' "$OAK_FORMAL_ROOT/split.json")
OAK_VAL_IDS=$(.venv/bin/python -c 'import json,sys; print(",".join(map(str,json.load(open(sys.argv[1]))["validation"])))' "$OAK_FORMAL_ROOT/split.json")

# 必须确认退出码为 0 且 precheck.json passed=true，再执行下一条。
.venv/bin/python -m datasets.locomo.scripts.precheck_agentic \
  --output "$OAK_FORMAL_ROOT" --arm g1 --convs conv-26

.venv/bin/python -m datasets.locomo.run \
  --output "$OAK_FORMAL_ROOT" --arm g1 --train-only --cases conv-26 \
  --optimization-mode wiki --scope sfcp --graph-rebuild \
  --train-question-ids "$OAK_TRAIN_IDS" --validation-question-ids "$OAK_VAL_IDS" \
  --proposal-attempts 3 --round-deadline-s 7200 --rounds 10
```

执行 agent 需将标准输出、错误输出和确切运行进程保存到本次目录，并监控实质进度。不要仅凭进程存活判断正常；若反复限流，暂停该进程并保留检查点，避免继续烧轮。当前 train-only 入口没有接入 STOP 文件，不能以写 STOP 文件代替停止确切进程。

正常完整评分后拒绝候选仍可继续下一轮。round_timeout、准入耗尽及评分不完整分别报告，不记成“完成一轮有效质量验证”。源码/模型/配置/题号完全相同时可在原命令末尾加 --resume，复用已有成功答案、评分和 Wiki；不要重新生成时间戳目录。已落盘的终止失败决策不会因 resume 自动重开，已经消耗的轮预算也不会重置。仅对已确认的限流/传输失败检查点做有备份、限定范围的恢复处理，禁止清空整个 answers 或判题缓存。若必须修框架代码，保存原运行并明确新身份，不能静默用修改后的代码恢复同一正式实验。

## 锁版后全题对比

循环结束后先核对 summary、逐轮 decision、validation.json 和 published/current.json；current 指针须与已采纳版本一致。采用通过验证的已发布版本，不拿最后一个未采纳候选代替。若本次未采纳任何候选，锁 B0、报告无迭代收益。

只做两臂：锁定 G1 和纯向量 V0，固定 K=30；两臂共用同一最终资产、同一冻结记忆/向量、模型、作答/审查及判题配置，允许检索流程不同。Wiki不参与正式答题。该比较衡量整套 agentic 检索相对一次向量检索的效果，不能单独证明图或 Wiki 的因果贡献。

```bash
# 继续沿用上面实际记录的 OAK_FORMAL_ROOT；不要生成一个新训练根目录。
OAK_FINAL_ASSETS=$(.venv/bin/python - "$OAK_FORMAL_ROOT" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()/"train"
summary=json.loads((root/"summary.json").read_text())
pointer=json.loads((root/"published/current.json").read_text())
assert pointer["version"]==summary["adopted_version"], "采纳指针与 summary 不一致，停止外测"
bundle=root/"published"/pointer["path"]
assert (bundle/"manifest.json").exists()
print(bundle)
PY
)

# 准备阶段补齐并冻结 conv-44 后，全部 10 个对话、1986 题。
OAK_ALL_CASES=conv-26,conv-30,conv-41,conv-42,conv-43,conv-44,conv-47,conv-48,conv-49,conv-50
OAK_V0_ROOT="$OAK_FORMAL_ROOT/final-v0"
OAK_G1_ROOT="$OAK_FORMAL_ROOT/final-g1"

# 串行两臂，降低限流；前一臂结束并核对后才跑后一臂。
.venv/bin/python -m datasets.locomo.scripts.external_test \
  --arm v0 --assets "$OAK_FINAL_ASSETS" --cases "$OAK_ALL_CASES" \
  --output "$OAK_V0_ROOT" --vector-k 30 --graph-rebuild

.venv/bin/python -m datasets.locomo.scripts.external_test \
  --arm g1 --assets "$OAK_FINAL_ASSETS" --cases "$OAK_ALL_CASES" \
  --output "$OAK_G1_ROOT" --vector-k 30 --graph-rebuild \
  --baseline "$OAK_V0_ROOT"
```

此入口会按原输出目录自动恢复；没有 --resume 参数。恢复仍需完全相同的资产、代码和配置；保存为 execution_error 的限流答案不会自动再答，须先做分类、备份和定点清理。不要循环重跑成功问题。两臂共同条件必须 compatible、delta_valid=true；全部题目的结果和故障均进入分母，不删题抬分。

## 交付与判定

交付一份报告，先写做了什么、结果如何、核心问题是什么，至少包含：

1. 实际覆盖的对话/题数、冻结摘要核对、B0 冷启动证据、完整评分轮数/故障轮数/提案失败数及耗时。
2. B0 至 R10 的训练/验证分数、验证是否真正运行、采纳或拒绝理由。训练涨分且验证未涨不能记为有效收益。B0 若有故障，单独区分故障减少与无故障条件下的质量改善。
3. Wiki 的准入/准出、运行/策略经验是否可追溯，后续提案是否真正引用并应用；只给有轨迹支撑的例子。记录实际生效的 S/F/C/P 差异，P.extract 变化不算。
4. B0 与最终图的节点/边数量、类型、属性和关系差异，配合真实召回轨迹说明是否利用了变化。S/图没变就报告没变，不能把 P/F 改善归因于图演化。
5. 两臂逐对话 original_precise/original_lenient、修订口径、故障、调用/时延/费用与配对差值。分别给 conv-26 的训练/验证结果、其他 9 个对话的宏/微平均、全部 10 个对话共 1986 题的汇总。conv-26 全题含训练题，不能当独立测试；其他对话仅称本轮未参与优化，不声称历史上从未看过。
6. 给三条独立结论：运行是否稳定、循环 Wiki 是否按设想发挥作用、验证及其他对话质量是否改善。79 题验证中多对一题约为 1.27 个百分点，反复选版后的一点提升不等于已证明泛化，须结合最终其他对话结果。若无收益或两臂条件不兼容，如实报告，不再用测试错题修改当前版本。

开始时间未固定，也不能保证扩大题量后的十轮和全部外测在一个凌晨内结束。保留完整恢复现场比赶时间刷失败轮重要；该文件不授权另开无关实验或额外消融。
