# OaK 收口设计（DESIGN-closeout）

> 配套 `docs/ARCHITECTURE.md`（框架四部件）。本文定义收口的目标、判定规则、迭代协议与冻结清单。
> 方向：oak 是主体（四步循环 + 内核），WikiSkill（arXiv:2608.27454）只借迭代纪律。

## 1. 北极星

**评测外（系统侧）失败 ≤ 6 道 / 100 题**（conv-44 158 题 → ≤ 9 题）。
不追求绝对分超历史最好，追求：目标驱动的多轮迭代 + 干净归因 + 评测器零改动。

## 2. 评测内 / 评测外判定

| 侧 | 含义 | 细分 | 举证要求 |
|---|---|---|---|
| 评测外（算系统的错） | 系统自己的问题 | extraction_miss / retrieval_miss / date_error / subject_error / bad_refusal / phrasing_or_reasoning / execution | 归因器自动分桶 |
| 评测内（不算，但须证据） | 评测基准的问题 | gold_not_in_transcript（译文没有）/ judge_dispute（实质等价被判错）/ gold_curation（策展语义） | 每题一句话证据 |

**模糊一律算评测外（保守，不放水）。** 归因提示词冻结（诊断用途，非优化对象）。

## 3. 迭代协议（每轮）

1. `snapshot rN`：快照资产面 + 记录评测器冻结集指纹
2. 提议器（驱动 agent，WikiSkill 式自选）读 wiki/patch-impact 与归因簇，做**一个**原子修复（单变量）
3. 可选小样冒烟（不好做可跳过）
4. `rerun rN`：**全量**重跑 conv-44（新 tag = fresh 目录；请求级缓存使未受影响调用近零成本；逐题 delta 使负面效果可见）
5. `gate rN`：归因 → 系统侧计数**严格下降**才 ACCEPTED（best 前移），否则 REJECTED + 自动回滚；冻结集指纹变化即拒绝；patch-impact.md 追加审计（diff + 逐题 fixed/broke + 被拒提案全文留档）
6. wiki 永不回滚：patterns/（一页一模式）、index、log 逐轮更新

## 4. 评测器冻结清单（σ 永不指向）

`datasets/locomo/pipeline/judge.py`、`prompts/judge.py`、`protocol.py`、`lenient_report.py`、`data/gold_repairs.jsonl`、`data/locomo10_zh.json`（数据集本体）。每轮 gate 校验指纹零变化。

## 5. 防自欺三件（门控不变量）

- **fresh 评估**：每轮新 tag 目录，不评陈旧产物
- **曝光可查**：修复的 diff 全文留档，可追溯是否到达相应角色请求（请求缓存按内容寻址，天然可证）
- **health 判 mistrial**：执行故障/网关抖动导致的中断不算资产的失败，重跑该轮

## 6. 数据分工（三段，用户定）

conv-26 = 训练/提案源｜conv-44 = 验证/门控战场（今夜在此）｜conv-30 = 测试（收口后一次性报）。

## 7. 收口完成判定

- 评测外 ≤ 9/158 且每轮可归因（patch-impact 完整）
- 评测器与数据集指纹零变化（state.json history 自证）
- 剩余失败全部带评测侧证据
- 终版报告 `runs/closeout/FINAL.md`：N₀→终态轨迹、每轮 fixed/broke、剩余失败举证
