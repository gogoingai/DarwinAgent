# 自定义任务

[English](../en/custom-tasks.md) · [简体中文](../zh-CN/custom-tasks.md) · [README](../../README.zh-CN.md)

任务接入只需输入适配与独立评测两个接口。下面是可保存为脚本的完整真实模型示例：先按[配置](configuration.md)在进程环境中设置三个连接变量；SDK 本身不会加载 `.env`。示例复用已打包的设备任务，换成新领域时需要同时替换来源类型、模式、资产和问题契约。使用新的输出目录。

```python
import asyncio
import time
from pathlib import Path
from darwinagent import (
    CaseInput, CorpusBlock, QuestionInput, SourceRef, EvaluationResult,
    Config, RunConfig, Pipeline, TaskSpec,
)
from darwinagent.demo import TASK_ROOT
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.client import LLMClient

class Records:
    def generation_input(self, case_id):
        return CaseInput(case_id,
            (CorpusBlock(SourceRef("maintenance_record", case_id, "row-1"),
                         "设备 D-17 于 2026-09-01 由林维护。"),),
            (QuestionInput("q1", "谁在什么时候维护了 D-17？",
                           {"serial": "D-17"}),))

class Score:
    async def evaluate(self, result):
        correct = sum(a.status == "answered" and "林" in a.answer
                      and "2026-09-01" in a.answer for a in result.answers)
        faults = sum(a.status == "execution_error" for a in result.answers)
        return EvaluationResult({"correct": correct}, len(result.answers),
                                len(result.answers) - faults, faults, 0)

async def main():
    work = Path("runs/custom-live")
    config = Config.from_env(work_dir=work)
    config.validate_model()
    config.max_http_requests = 40
    config.request_budget_path = work / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800
    # Start with the packaged task. For another domain, register your own root.
    bundle = load_assets(TASK_ROOT).export(work / "assets")
    spec = TaskSpec.load(TASK_ROOT / "task.yaml", bundle)
    client = LLMClient(config)
    try:
        result = await Pipeline(client, work / "generation").run(
            Records().generation_input("device-custom"), spec, RunConfig())
        score = await Score().evaluate(result)
        print(result.to_dict())
        print(score.to_dict())
    finally:
        await client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
```

此示例验证 SDK 接入，不会启动优化循环。需要零网络运行时，直接执行已验证的完整离线示例：

```bash
uv run python examples/third_domain.py
```

其输出包括 `林于 2026-09-01 维护了设备 D-17。`；模型响应是显式录制的，评分由示例评测器按答案计算。

## 登记资产

任务根目录必须有 `task.yaml` 和 `assets/index.yaml`。两者均采用声明式登记，不导入任务执行模块。参考[打包示例](../../src/darwinagent/demo/device_maintenance/task.yaml)。目录通常为 `assets/{S,F,C,P}/`。

| 类型 | 契约 |
| --- | --- |
| S | 恰好一个模式资产 |
| F | 至少一个查询函数；输入／输出契约、模式依赖与试跑参数必须登记 |
| C | 可选；stage 为 graph 或 answer；返回一致的 `{ok: bool, issues: [string]}` |
| P | extract、tools、answer、review 四个固定角色各一个；只使用已登记槽位 |

`load_assets(root).export(path)` 拒绝覆盖非空资产目录；`TaskSpec.load(root / "task.yaml", bundle)` 将声明和版本绑定。F/C 使用受限执行能力；评测参考不可进入 `CaseInput`、资产、提案训练输入或查询结果。真实留出实验还需要冻结不相交的集合和独立评测协议，见[实验指南](experiments.md)。
