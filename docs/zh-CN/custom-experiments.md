# 让自己的任务进入优化循环

[English](../en/custom-experiments.md) · [返回中文首页](../../README.zh-CN.md)

[自定义任务](custom-tasks.md)展示了一次问答与评分。本页把同样的记录、问题与评测规则交给 `ExperimentRunner`，执行基线、提案、候选评测、采纳和经验记录。

先完成[安装与模型配置](quickstart.md)。在 `.env` 所在目录创建 `custom_experiment.py`，复制以下完整脚本。它可以独立运行，不依赖其他样例文件。

```python
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import (
    AdoptionPolicy,
    CaseInput,
    Config,
    CorpusBlock,
    EvaluationResult,
    ExperimentRunner,
    QuestionInput,
    RunConfig,
    SourceRef,
    TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets


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
    output = Path("runs/custom-experiment").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    seed = output / "seed"
    load_assets(TASK_ROOT).export(seed)

    def evaluator_factory(_client, _path):
        return Score()

    evaluator_factory.criterion_id = "maintenance-fields-v1"
    runner = ExperimentRunner(
        Records(),
        evaluator_factory,
        config,
        RunConfig(protocol_attempts=2, answer_attempts=2, tool_steps=3, max_tokens=1800),
        AdoptionPolicy("correct", ()),
        output,
        optimization_mode="wiki",
        wiki_call_limit=6,
        proposal_attempts=2,
        seed_assets=seed,
    )
    async with asyncio.timeout(1800):
        summary = await runner.run(
            "device-custom",
            TaskSpec.load(TASK_ROOT / "task.yaml"),
            rounds=2,
            scope=("P",),
        )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{output}")


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```bash
python custom_experiment.py
```

## 如何改成自己的任务

- `Records` 提供你自己的原始记录、问题和查询参数。
- `Score` 定义自己的评分规则，参考答案留在评测器内部。
- `TASK_ROOT` 指向任务声明与资产；新领域需要按[资产接入步骤](custom-tasks.md#3-换成全新的领域)修改它们。
- `AdoptionPolicy("correct", ())` 要求 `correct` 指标严格提升；同分会被拒绝。
- `scope=("P",)` 将本例的修改范围限制为角色提示词；`rounds=2` 最多执行两轮。
- `output` 为独立实验选择新目录；重复运行前换目录，避免覆盖已有种子资产。

结果保存在 `runs/custom-experiment/`。首先查看 `summary.json`，了解每轮是否采纳和原因；再看 `B0/evaluation/`、`R1/evaluation/`、`optimization/wiki.json`、`published/current.json` 与 `http_attempts.json`。

这是真实模型调用，最多 40 次请求尝试、1800 秒。候选可能被拒绝；完成不表示质量必然提升。本例只有一个问题，没有留出集，字符串匹配也只是入门评分方法。正式评测需要自己的数据划分和规则，见[实验协议](experiments.md)。

[英文样例源文件](../../examples/custom_experiment.py)提供相同接口与英文记录。
