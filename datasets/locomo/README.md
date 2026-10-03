# datasets/locomo —— 中文 LoCoMo 本体问答（OaK，零向量）

> 当前入口使用 Oak 0.4.0 的共同 Pipeline（原子事实两阶段 + 事实锚定图）。LoCoMo 只实现输入适配和独立评测；生成只使用原始对话文本、说话人与会话日期。三集合协议：训练 conv-26（199 题，四口径，修订 gold 严格为主指标）、验证 conv-47（190 题，原始 gold 严格+宽松）、测试 conv-49（196 题，同验证口径）。流程：真实模型预检 → B0 门（全完+零故障）→ Rn 无限训练迭代（操作者叫停）→ 候选锁定 → 统一验证选版 → 测试一次性揭盲；题次逐题记账。实验根 `runs/atomic_v1/`，历史成绩保留、不作新框架基线。

框架目录、类图和资产能力边界见 [架构说明](../../docs/ARCHITECTURE.md)，冷启动与恢复方法见 [使用说明](../../docs/PORTABLE-USAGE.md)。

## 目录结构

```
datasets/locomo/
├── REPORT.md          # 报告（论文模板：摘要/本体/实验/归因/结论）——先看这个
├── data/              # 数据集
│   ├── locomo10_zh.json     # 中文版主数据集（10 段对话 / 5,882 条消息 / 1,986 题）
│   ├── locomo10.json        # 英文原版
│   ├── gold_repairs.jsonl   # gold 修复表（18 条，逐题判据 + 原文引证，判分双口径）
│   └── DATASET_CARD.md      # 数据集卡片（翻译口径说明）
├── adapter.py         # LocomoAdapter：三层语料、日期、说话人、问题与来源转换
├── evaluator.py       # LocomoEvaluator：独立参考与冻结四口径评测
├── exports.py         # 纯格式转换
├── run.py             # 装配共同 ExperimentRunner/Pipeline
├── pipeline/          # 保留的冻结评测实现与历史说明；不实现新生成流程
│   ├── judge.py / protocol.py / prompts/
│   ├── analyze.py / closeout.py / lenient_report.py
│   └── OPTIMIZATION_LOG.md / ARCHIVE.md / PLAN-90.md
└── runs/              # 复现产物（图 / 答案 / 报告 / 失败归因 / LLM 缓存 / 台账）
    └── frozen/              # 冻结版：schema + 主题词表 + 最优轮报告（iter22）
```

## 作答与评测修复

旧固定图审计记录见 [pipeline/EVALUATION_V1.md](pipeline/EVALUATION_V1.md)。新实验独立生成并使用同一冻结评分口径；生成接口不提供参考答案。此前宽松结果混用了修复与原始 gold，不能作为新口径基线或官方评测复现。

## 快速开始

```bash
# 冷启动 B0 + 两轮资产提案，完整 conv-26；使用一个新的输出目录
uv run python -m datasets.locomo.run --experiment --output datasets/locomo/runs/my_cold_start

# 同阶段、同资产、同模型配置和同代码身份的断点恢复
uv run python -m datasets.locomo.run --experiment --resume --output datasets/locomo/runs/my_cold_start

# 历史结果的独立复评入口仍保留
uv run python -m datasets.locomo.pipeline.lenient_report conv-26 iter22
```

## 三份核心文档

| 想了解 | 看 |
|---|---|
| 成绩、本体定义、失败归因 | [REPORT.md](REPORT.md) |
| 23 轮迭代怎么爬到 79.9%（每轮改了什么、负结果） | [pipeline/OPTIMIZATION_LOG.md](pipeline/OPTIMIZATION_LOG.md) |
| 为什么 90% 不可达（数据噪声证据链） | [pipeline/PLAN-90.md](pipeline/PLAN-90.md) |

## 注意

- 数据纪律（评测有效性前提）：当前固定图包含 observation/event_summary 标注层，需披露；作答只见问题与图；判分盲判——详见 [pipeline/README.md](pipeline/README.md)。
- 中文数据集 QA 与对话分开翻译，存在系统性噪声（图片-only 细节 / gold-语料矛盾 / 术语漂移），天花板审计与 18 条修复见 `pipeline/OPTIMIZATION_LOG.md`。
- 完整产物（含 LLM 请求缓存，可零 API 费用复现全部轨迹）：<https://huggingface.co/datasets/justis-xu/oak-locomo>
