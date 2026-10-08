# 快速开始

[English](../en/quickstart.md) · [返回中文首页](../../README.zh-CN.md)

本指南从空目录开始，先配置真实模型，再分别展示命令行与 Python 调用。没有模型密钥时，可直接跳到[离线回放](#5-没有模型时先做离线回放)。

## 1. 安装

需要 Python 3.11 或更新版本。先创建项目目录和虚拟环境：

```bash
mkdir darwinagent-starter
cd darwinagent-starter
```

macOS 或 Linux 激活环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell 激活环境：

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

安装同一个包即可获得命令行工具和 Python 接口：

```bash
python -m pip install darwinagent
darwinagent --version
```

当前发行版本为 `0.1.1`。后续命令都在这个项目目录、已激活的环境中运行。

## 2. 配置你自己的模型

在 `darwinagent-starter/` 下创建 `.env` 文件，与 `.venv` 并列，填写：

```dotenv
DARWINAGENT_BASE_URL=https://your-provider.example/v1
DARWINAGENT_MODEL=your-model-name
DARWINAGENT_API_KEY=your-api-key
```

三个值都是占位符，必须替换。地址来自模型提供商的接口文档，模型名必须是该接口支持的名称，密钥使用你自己的密钥。地址填写 OpenAI 兼容接口的根地址，不包含 `/chat/completions`；框架会添加请求路径。所有角色默认共用这一个模型。

命令行读取当前目录 `.env`；已有环境变量优先。不要提交 `.env` 到仓库。SDK 示例会显式加载它。[更多配置说明](configuration.md)。

先检查安装和配置是否齐全，这一步不请求模型：

```bash
darwinagent doctor --output runs/doctor
```

如果配置齐全，会出现 `Live model configuration: ready`。这只表示字段齐全。要验证地址、密钥和模型是否能实际连接，再执行：

```bash
darwinagent doctor --check-model --output runs/doctor
```

连接检查会发送一次真实模型请求，最多等待 30 秒；成功会出现 `Model endpoint OK`。

## 3. 用命令行运行完整演示

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

内置任务是一条设备维护记录和一个问题：“谁在什么时候维护了 D-17？”模型抽取记录、查询证据并生成答案；独立评测器检查维护人员与日期。框架以此运行基线、提出提示词修改、评测两轮候选，保存采纳或拒绝决定及经验。

| 参数 | 含义 |
| --- | --- |
| `--mode live` | 调用你配置的真实模型 |
| `--rounds 2` | 最多执行两轮候选；命令行支持 0–10 轮 |
| `--output runs/demo-live` | 保存结果的目录 |
| `--max-requests 40` | 最多发出 40 次请求尝试，包含重试 |
| `--timeout 1800` | 整次运行最多 1800 秒 |

完成时，终端结果包含 `status: "complete"` 和每轮的 `decisions`。真实模型的基线可能已全部答对，同分候选会被拒绝；评分不一定上涨。这个微型演示不包含验证集或测试集。

## 4. 在 Python 项目中调用

在同一个目录创建 `main.py`，复制以下**完整脚本**。它使用刚才安装的包和同一份 `.env`，无需检出仓库：

```python
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import Config
from darwinagent.demo import run_demo


async def main():
    load_dotenv(Path(".env"))
    output = Path("runs/python-live").resolve()
    config = Config.from_env(work_dir=output)
    config.validate_model()
    config.max_retries = 2
    config.max_http_requests = 40
    config.request_budget_path = output / "http_attempts.json"
    config.deadline_monotonic = time.monotonic() + 1800

    async with asyncio.timeout(1800):
        summary = await run_demo(output, mode="live", rounds=2, config=config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{output}")


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```bash
python main.py
```

结果位于 `runs/python-live/`，与命令行演示分开。`load_dotenv()` 是脚本显式执行的步骤；`Config.from_env()` 本身只读取进程环境变量。SDK 调用必须把 `config` 传给真实模式。

这个脚本运行内置演示；要使用自己的数据，继续阅读[自定义任务指南](custom-tasks.md)。可下载的[对应样例](../../examples/live_demo.py)使用相同逻辑。

重新运行这个脚本时，若要接着已有进度运行，在 `run_demo()` 中添加 `resume=True`；若要重新实验，把 `output` 改为新目录。

## 5. 没有模型时先做离线回放

无需 `.env`，也不会发送模型请求：

```bash
darwinagent demo --mode replay --rounds 2 --output runs/demo-replay
```

预期结果：`status: "complete"`、两轮的 `accepted` 依次为 `true`、`false`、`http_attempts: 0`。第一轮候选被采纳，第二轮因同分被拒绝。独立评分按一个合成问题的两个字段得到 0.5 → 1.0 → 1.0。

回放使用预设模型响应、预置基线和只修改提示词的提案，实际执行运行器与评测流程。它用于检查机制，不表示真实模型能力提升。

Python 离线入口同样无需配置：

```python
import asyncio
from pathlib import Path

from darwinagent.demo import run_demo

summary = asyncio.run(run_demo(Path("runs/python-replay"), mode="replay", rounds=2))
print(summary)
```

## 6. 查看结果和续跑

先打开 `demo-summary.json` 查看运行状态、每轮评分、是否采纳及原因。需要核查细节时，再看：

| 相对输出目录的路径 | 内容 |
| --- | --- |
| `experiment.json` | 初始实验声明 |
| `B0/evaluation/maintenance-demo.json` | 基线评分 |
| `R1/evaluation/maintenance-demo.json` | 第一轮候选评分，第二轮在 `R2` |
| `R1/optimization/attempt-0/proposal-call.json` | 提案的输入与输出 |
| `optimization/wiki.json` | 保存的经验与决策 |
| `published/current.json` | 保留的资产版本 |
| `http_attempts.json` | 真实模式的实际请求尝试数 |

例如继续刚才的真实演示：

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800 --resume
```

`--rounds` 表示目标总轮数，完成两轮后再以两轮续跑不会新增两轮；改为 `--rounds 3` 才会继续第三轮。

建议先用相同模式与配置续跑。日常续跑可记录模型和执行条件的变化；需要原始条件下的严格比较时，加 `--strict-comparison`。换任务、换数据或重新比较时，用新目录。更多说明见[人工干预与续跑](../workspace-continuation.zh-CN.md)。

## 7. 常见问题

| 遇到的问题 | 处理方法 |
| --- | --- |
| 找不到 `darwinagent` 命令 | 激活安装时使用的虚拟环境，再执行 `python -m pip show darwinagent` 确认安装位置 |
| 提示缺少模型配置 | 检查当前目录是否有 `.env`，三个值是否填写；Python 中还需显式加载 |
| `doctor` 成功，真实运行仍失败 | 默认检查不联网；用 `--check-model` 验证实际连接 |
| 401／403 | 检查密钥与该接口的访问权限 |
| 404／找不到模型 | 检查根地址是否重复包含请求路径，以及模型名是否受支持 |
| 达到请求上限或超时 | 查看请求记录和报错；排除连接问题后按需要调整限额，再用 `--resume` 续跑 |
| 输出目录已存在 | 使用 `--resume` 继续，或换一个目录开始新实验 |
| 所有候选都被拒绝 | 检查评分和拒绝原因；同分拒绝符合采纳规则 |

## 从源码开发

普通用户使用 PyPI 安装即可。贡献者需要仓库中的 `examples/`、测试和开发工具时：

```bash
git clone https://github.com/gogoingai/DarwinAgent.git
cd DarwinAgent
uv sync --frozen
uv run python examples/third_domain.py
```

`examples/third_domain.py` 是仓库内的离线任务样例，核心安装包不包含这个文件。后续开发检查见[贡献指南](../../CONTRIBUTING.zh-CN.md)。
