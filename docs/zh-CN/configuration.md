# 模型配置

[English](../en/configuration.md) · [返回中文首页](../../README.zh-CN.md)

真实模型运行必须配置接口地址、模型名称和密钥。默认所有角色共用一个 OpenAI 兼容对话接口，无需分别选择多个模型。离线回放不需要配置。

## 命令行：创建 .env

在你运行命令的目录创建 `.env`，填写：

```dotenv
DARWINAGENT_BASE_URL=https://your-provider.example/v1
DARWINAGENT_MODEL=your-model-name
DARWINAGENT_API_KEY=your-api-key
```

这些值都是占位符。请从提供商获取自己的配置：

| 环境变量 | 填什么 |
| --- | --- |
| `DARWINAGENT_BASE_URL` | OpenAI 兼容接口的根地址，通常以 `/v1` 结尾；不包含 `/chat/completions` |
| `DARWINAGENT_MODEL` | 该接口支持的准确模型名称 |
| `DARWINAGENT_API_KEY` | 该接口的密钥 |

框架使用对话补全接口并读取流式响应。端点需要支持相应请求格式；仅支持其他接口格式的服务不能直接接入。

命令行只读取**当前工作目录**的 `.env`，不会自动寻找脚本目录或仓库根目录。已有环境变量优先；如果旧环境变量与文件不一致，先清理旧值或更新它们。不要提交密钥。

检查字段齐全：

```bash
darwinagent doctor --output runs/doctor
```

验证实际连接：

```bash
darwinagent doctor --check-model --output runs/doctor
```

第一条不请求模型；第二条发送一次真实请求，最多等待 30 秒。配置齐全不代表密钥、模型和网络有效。[错误排查](quickstart.md#7-常见问题)。

## Python：显式加载配置

SDK 不自动读取 `.env`。在创建配置前加载文件，然后校验：

```python
from pathlib import Path

from dotenv import load_dotenv

from darwinagent import Config

load_dotenv(Path(".env"))
config = Config.from_env(work_dir=Path("runs/my-project").resolve())
config.validate_model()
```

已有程序如果通过环境变量管理密钥，无需再加载文件。也可以显式构造 `Config(api_base_url=..., api_key=..., model_strong=..., work_dir=...)`；未单独指定的其他档位使用主模型。

`Config` 管模型连接，`RunConfig` 管任务执行规则。它们都不是可优化的任务资产。真实运行示例见[Python 快速开始](quickstart.md#4-在-python-项目中调用)。

## 请求和时间限制

命令行演示使用：

```bash
darwinagent demo --mode live --rounds 2 --output runs/demo-live \
  --max-requests 40 --timeout 1800
```

请求上限按实际发出的 HTTP 尝试计数，包含重试；一次任务通常需要多次模型调用。`--timeout` 限制整个演示的运行时间。请求数不等于词元数，也不等于费用。

SDK 可显式设置：

```python
import time

config.max_retries = 2
config.max_http_requests = 40
config.request_budget_path = config.work_dir / "http_attempts.json"
config.deadline_monotonic = time.monotonic() + 1800
config.request_timeout_s = 120
```

多个客户端需要共用 `request_budget_path` 才能统一计数。`deadline_monotonic` 是请求的绝对截止时间，覆盖排队、流式响应与重试；`request_timeout_s` 约束传输等待。还需像[完整样例](quickstart.md#4-在-python-项目中调用)一样，用 `asyncio.timeout(...)` 限制整次任务，包括模型调用之外的处理时间。

## 高级配置：不同角色使用不同模型

第一次使用保持默认的单模型即可。需要单独配置抽取与工具角色时，可以在 SDK 中覆盖：

```python
import os

config.model_fast = "your-fast-model"
config.fast_base_url = "https://fast-provider.example/v1"
config.fast_api_key = os.environ["OPTIONAL_FAST_API_KEY"]
config.role_tiers = {**config.role_tiers, "extraction": "fast", "tools": "fast"}
```

`OPTIONAL_FAST_API_KEY` 是该片段自己读取的变量，不是命令行自动加载的配置项。中间档位可用 `model_middle`、`middle_base_url`、`middle_api_key` 覆盖；其余角色继续使用主连接。

厂商专用参数默认关闭。命令行可通过 `--model-profiles` 或 `DARWINAGENT_MODEL_PROFILES=true` 启用，SDK 使用 `config.model_profiles = True`。仅在所选端点支持对应参数时启用；`Config.from_env()` 本身只加载三个连接变量，不加载该开关。

重新比较模型或路由时建议使用新实验目录；保留进度的日常续跑与严格比较规则见[续跑说明](quickstart.md#6-查看结果和续跑)。
