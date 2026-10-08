# 使用自己的数据和任务

[English](../en/custom-tasks.md) · [返回中文首页](../../README.zh-CN.md)

先按[快速开始](quickstart.md)安装包并配置 `.env`。本指南先替换设备维护任务的输入，再说明如何接入新领域。

## 1. 用自己的记录运行一次问答

在 `.env` 所在目录新建 `custom_task.py`，复制以下完整脚本。无需下载仓库，任务声明与资产从已安装的包中加载。

这个样例包含三部分：`Records` 提供记录和问题；`Pipeline` 用模型抽取、查询并回答；`Score` 根据实际答案独立评分。

```python
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import (
    CaseInput,
    Config,
    CorpusBlock,
    EvaluationResult,
    Pipeline,
    QuestionInput,
    RunConfig,
    SourceRef,
    TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient


class Records:
    def generation_input(self, case_id):
        return CaseInput(
            case_id,
            (
                CorpusBlock(
                    SourceRef("maintenance_record", case_id, "row-1"),
                    "设备 D-17 于 2026-09-01 由林维护。",
                ),
            ),
            (QuestionInput("q1", "谁在什么时候维护了 D-17？", {"serial": "D-17"}),),
        )


class Score:
    async def evaluate(self, result):
        correct = sum(
            answer.status == "answered" and "林" in answer.answer and "2026-09-01" in answer.answer
            for answer in result.answers
        )
        faults = sum(answer.status == "execution_error" for answer in result.answers)
        return EvaluationResult(
            {"correct": correct},
            len(result.answers),
            len(result.answers) - faults,
            faults,
            0,
        )


async def main():
    load_dotenv(Path(".env"))
    output = Path("runs/custom-live").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    bundle = load_assets(TASK_ROOT).export(output / "assets")
    spec = TaskSpec.load(TASK_ROOT / "task.yaml", bundle)
    async with asyncio.timeout(1800), LLMClient(config) as client:
        result = await Pipeline(client, output / "generation").run(
            Records().generation_input("device-custom"), spec, RunConfig()
        )
    score = await Score().evaluate(result)
    report = {
        "answers": [
            {
                "question_id": answer.question_id,
                "status": answer.status,
                "answer": answer.answer,
            }
            for answer in result.answers
        ],
        "evaluation": score.to_dict(),
        "output": str(output),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```bash
python custom_task.py
```

脚本会输出答案和评分，结果保存在 `runs/custom-live/`。如果模型正确回答维护人员与日期，`metrics.correct` 为 `1`；否则为 `0`，故障另行计数。这是便于理解的字符串匹配评测，真实业务应提供自己的评测规则。

样例会调用真实模型，限制为 40 次请求尝试、1800 秒。再次独立运行时换一个输出目录；资产导出不会覆盖非空目录。可下载的[英文样例文件](../../examples/custom_task.py)使用同一接口，并提供英文记录和问题。

## 2. 修改哪些地方

| 部分 | 你需要做什么 |
| --- | --- |
| `CorpusBlock.text` | 换成要交给模型的原始记录 |
| `SourceRef` | 给记录提供来源类型、文档编号和片段编号 |
| `QuestionInput` | 提供问题编号、问题文本与查询参数 |
| `Score.evaluate()` | 用自己的参考答案或业务规则评测实际输出 |
| `output` | 为每次独立运行选择单独目录 |

同一设备领域可以先修改记录内容和问题。这个任务的查询参数是 `serial`，来源类型是 `maintenance_record`，记录包含设备编号、维护日期、维护人员；它们与已登记资产的契约对应。评测用的参考答案留在 `Score` 内部，不加入模型的输入。

**这个脚本只运行一次任务并评分。** 若要体验“基线 → 提案 → 候选评测 → 采纳 → 经验记录”的循环，使用[快速开始中的真实模型演示](quickstart.md#4-在-python-项目中调用)。让自己的任务进入循环，直接复制[自定义优化实验的完整样例](custom-experiments.md)。独立评测与留出集规则见[实验协议](experiments.md)。

## 3. 换成全新的领域

新领域还需提供自己的任务声明与 S/F/C/P 资产，不能只替换问题文本。可以先复制已安装的设备样例到当前项目：

```python
from pathlib import Path
from shutil import copytree

from darwinagent.demo import TASK_ROOT

copytree(TASK_ROOT, Path("my-task"))
```

在上述脚本中，把 `TASK_ROOT` 替换为 `Path("my-task")`，然后修改复制出的文件：

```text
my-task/
├── task.yaml
└── assets/
    ├── index.yaml
    ├── S/schema.yaml
    ├── F/lookup.py
    ├── C/subject.py
    └── P/
        ├── extract.txt
        ├── tools.txt
        ├── answer.txt
        └── review.txt
```

| 文件／资产 | 作用与要求 |
| --- | --- |
| `task.yaml` | 声明任务名称、来源类型、查询参数和答案格式 |
| `assets/index.yaml` | 登记资产编号、文件路径、契约及依赖 |
| S：模式 | 定义实体、属性与关系；恰好一个 |
| F：查询函数 | 从图中查询证据；至少一个，登记输入／输出与试跑参数 |
| C：任务检查 | 可选，在图或答案阶段检查；返回 `{ok: bool, issues: [string]}` |
| P：角色提示词 | 抽取、工具、回答、审查四个角色各一个，仅使用已登记槽位 |

加载采用声明式登记，不导入任务执行模块。`load_assets(root).export(path)` 导出独立资产版本；`TaskSpec.load(root / "task.yaml", bundle)` 绑定任务与版本。函数和检查在受限环境中执行。

为新领域同时修改输入、模式、查询、提示词和评测器。评测参考不能进入生成输入、资产或查询结果。内置演示只包含一个合成问题；正式实验还需不相交的训练、验证、测试集。

## 仓库中的离线样例

需要研究离线接口时，先检出源码，再执行：

```bash
uv sync --frozen
uv run python examples/third_domain.py
```

[离线脚本](../../examples/third_domain.py)使用录制响应，不请求模型；普通 `pip install darwinagent` 不会安装仓库根目录的 `examples/`。
