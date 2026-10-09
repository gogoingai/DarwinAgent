# 中文 LoCoMo 接入

原始数据从 [justis-xu/memory-eval-zh](https://huggingface.co/datasets/justis-xu/memory-eval-zh/tree/main/locomo) 加载。命令指定 HF 仓库和版本，程序自动下载所需的中文、英文两个原始文件，并在输出目录的 `dataset-source.json` 中记录实际提交与文件校验值。继续运行使用已锁定的提交；HF 缓存目录可以更换。数据文件、事实、图、模型回执和实验结果不纳入 Git。

## 运行

先安装基准接入依赖，并设置操作者提供的 `DARWINAGENT_BASE_URL`、`DARWINAGENT_MODEL`、`DARWINAGENT_API_KEY`。

```bash
uv sync --extra benchmarks

# 从 HF 原始对话冷启动，按当前 S 动态构图，开放 S/F/C/P 两轮提案
uv run python -m datasets.locomo.run \
  --dataset-repo justis-xu/memory-eval-zh --dataset-revision main \
  --arm g1 --train-only --cases conv-26 --rounds 2 \
  --output runs/locomo-example

# 复用同一输出目录中锁定的数据提交、事实和成功回执
uv run python -m datasets.locomo.run \
  --dataset-repo justis-xu/memory-eval-zh --dataset-revision main \
  --arm g1 --train-only --cases conv-26 --rounds 2 --resume \
  --output runs/locomo-example
```

`--dataset-revision` 可指定完整提交以便复现。`--dataset-cache` 可指定缓存位置；已有运行记录或完整提交配合 `--dataset-offline` 可仅使用缓存。`--data-dir` 是显式本地输入选项，与 `--dataset-repo` 互斥。没有数据源参数时直接报错。

## 事实与图

新 g1 运行只从消息正文、说话人与会话日期抽取事实，保存到 `--output` 下的 `memory/`，供之后的 S/P 修改复用。程序按当前 S 与 P.extract 用 LLM 生成实体、属性和关系，校验每个节点和边的来源证据。Wiki 与提案器共同查询事实、图、工具和作答轨迹，判断应修改 S/F/C/P 中的哪些资产。见[动态构图与资产修改信号](../../docs/zh-CN/dynamic-graph.md)。

仅提供对话模型时，新记忆包声明 `vector_mode: none`，运行图检索。需要已有事实或向量索引时，通过 `--memory-root` 显式传入记忆包目录；固定图重放 `--graph-mode frozen`、固定投影 `--graph-mode projection` 和 v0 臂均要求该参数。没有默认历史快照，也不会自动寻找别的实验目录。

## 独立评测

默认使用原始 QA 的精准、宽松两项指标。`--audited-reference` 可显式加入外部修订参考；修订参考缺失会报错。生成和构图不读取答案、证据题号、题型、摘要标注或 QA 派生槽位数据。

判题继承原评分原语：上下文包含原始对话中可用的派生标注和图片说明，范围宽于生成输入；仍以当前 gold 判分，来源冲突单独披露。当前接口锁为 `evaluation_lock.governance-20261009.json`，旧锁及历史结果保留。新运行的数据校验值和评测实现锁保存在输出目录，历史成绩不能直接作为新路径的基线。

旧实验报告见 [REPORT.md](REPORT.md)，历史产物索引见 [ARCHIVE.md](ARCHIVE.md)。历史实验产物与本次原始数据源分别管理；旧本体、图和答案不会成为新运行的隐式输入。
