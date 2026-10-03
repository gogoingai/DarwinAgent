# Oak 0.3.2 使用与仓库外接入

发行包只包含 `oak`。每个数据集实现 `DatasetAdapter.generation_input(case_id)` 与 `Evaluator.evaluate(RunResult)` 两个接口，资产和 TaskSpec 由声明文件加载。

```python
from pathlib import Path
from oak.config import RunConfig
from oak.engine import Pipeline
from oak.kernel import TaskSpec
from oak.kernel.registration import load_assets

bundle = load_assets(Path("my_task")).export(Path("run/assets"))
spec = TaskSpec.load(Path("my_task/task.yaml"), bundle)
case = adapter.generation_input("case-1")
result = await Pipeline(model_client, Path("run/generation")).run(case, spec, RunConfig())
scores = await evaluator.evaluate(result)
```

适配器返回标准 `CaseInput/CorpusBlock/SourceRef/QuestionInput`，请求参数符合 TaskSpec 的封闭契约；不能带评测字段或调用模型。框架固定选择两个 Agent、工具收集、候选、检查、审查、反馈重试和发布。不能提供 builder/answerer 回调或通过继承换掉数据集执行流程。

`AnswerResult.status` 只有 `answered/abstained/execution_error`。执行错误不携带答案；正常答案必须有真实来源和图节点出处。结果转换不会修补语义内容。

## 仓库内入口

离线第三任务（明确使用录制响应，验证框架执行，不代表真实模型性能）：

```sh
.venv/bin/python -B examples/third_domain.py
```

TravelPlanner 共用 Pipeline：

```sh
uv sync --extra benchmarks  # 原官方评测需要 gradio/pandas/numpy 等依赖
.venv/bin/python -B -m datasets.travelplanner.run --split train --index 0 --output datasets/travelplanner/runs/v032-smoke
```

LoCoMo 从已有新契约资产包运行：

```sh
.venv/bin/python -B -m datasets.locomo.run --case conv-26 --assets /absolute/path/to/bundle --output datasets/locomo/runs/v032-run
```

LoCoMo 无历史种子的完整两轮实验：

```sh
PYTHONHASHSEED=0 .venv/bin/python -B -u -m datasets.locomo.run --experiment --output datasets/locomo/runs/framework_v032_cold_20261003
```

同一阶段、同一代码/数据/模型/资产/预算身份恢复需要追加 `--resume`。变更身份必须使用新目录，不能沿用缓存。旧 0.2 的带 H bundle 和旧生成入口不适用于新 Runtime；历史运行及冻结代码保留。

上面的真实实验已经按两轮上限停止，结果为失败。B0/R1 各有 199 条执行错误，R2 提案准入失败，没有有效评分基线。该目录只用于查看原始记录；本次末尾清理了兼容数据类型文件的空行，精确原源码及输入保存在该目录的 `frozen_code/`，不在当前工作区强行恢复旧身份。新实验使用新目录。

## 资产格式与约束

`tasks/<task>/assets/index.yaml` 登记资产 ID、类型、源码位置、S 依赖、契约和 F 试跑参数。位置由框架解析，优化提案不含路径。Schema 是 YAML 声明；函数必须 `def run(params):`；检查必须 `def check(candidate):` 并返回 `{ok: bool, issues: [str]}`；提示只有固定角色和插槽。受限语法与能力列表见 [架构](ARCHITECTURE.md)。不支持的源码会拒绝，不能放宽权限让旧模块直接执行。

新底层算子、模型路由、预算或接入修复属于工程版本，不是资产优化。优化器只会创建候选资产版本，核心、接入代码、评测器、数据和配置指纹保持不变。

## 仓库外验收

实际验收在 `/private/tmp/oak-v03-portability/` 完成：独立虚拟环境安装 `oak_repro-0.3.2`，只拷贝第三任务的声明/资产和两个接口的示例，在 `python -I` 且移除 PYTHONPATH 后运行。ExtractionAgent、AnswerAgent、S/F/C/P 与固定 Pipeline 均实际执行，不依赖数据集包、仓库根或私有任务流程。

重新构建：

```sh
uv build --wheel
```

已验证 wheel 位于 `dist/oak_repro-0.3.2-py3-none-any.whl`。安装和复制任务资产后，普通应用直接调用 Pipeline；只有初始化与训练迭代才调用 ExperimentRunner。
