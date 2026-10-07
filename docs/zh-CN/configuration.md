# 配置

[English](../en/configuration.md) · [简体中文](../zh-CN/configuration.md) · [README](../../README.zh-CN.md)

默认连接采用一个 OpenAI 兼容 Chat Completions 端点，所有角色与档位使用同一模型；没有默认厂商地址或模型。真实运行前必须填写：

| 环境变量 | 含义 |
| --- | --- |
| `DARWINAGENT_API_KEY` | 端点凭据 |
| `DARWINAGENT_BASE_URL` | 提供商 API 根地址 |
| `DARWINAGENT_MODEL` | 该端点可用的模型名称 |
| `DARWINAGENT_MODEL_PROFILES` | CLI 中可选厂商参数配置，默认 false |

CLI 读取当前目录 `.env`，不覆盖环境变量；导入 SDK 不读取 `.env`、不创建客户端、不写文件。SDK 使用者需要先在进程环境中提供连接变量，再显式创建配置：

```python
import time
from pathlib import Path
from darwinagent import Config, RunConfig

config = Config.from_env(work_dir=Path("runs/custom"))
config.max_http_requests = 40
config.request_budget_path = config.work_dir / "http_attempts.json"
config.deadline_monotonic = time.monotonic() + 1800
config.request_timeout_s = 120
config.model_profiles = False
run_config = RunConfig(tool_steps=5, calls_per_question=32)
```

配置片段本身不发请求。多个角色客户端应共用 `request_budget_path`，才能统一计数。`deadline_monotonic` 是整个请求的绝对截止时间，覆盖排队、流式响应和重试；`request_timeout_s` 还约束传输等待。SDK 运行器需要调用者自行为整个任务设置总时间限制；CLI 的 `--timeout` 已覆盖整个演示。

高级用法可以设置 `model_strong`／`model_middle`／`model_fast`、对应端点／凭据和 `role_tiers`。`model_profiles=True` 是显式启用的兼容选项，不是默认的推理参数。历史数据集路由通过数据集边界的 legacy 配置显式加载，不能替代通用配置。

`Config` 管模型连接，`RunConfig` 管固定执行限额；两者都不是可优化资产。模型、路由或源码发生变化，应创建新的实验目录。[快速开始](quickstart.md)提供 CLI 配置方法。

## 显式高级覆盖

下面只创建配置，不创建客户端、不发请求。端点／模型为占位符；`OPTIONAL_FAST_API_KEY` 是示例自己读取的变量，不是通用 CLI 自动加载的配置项。替换后，抽取与工具角色走 fast，其余角色继续使用默认主连接。

```python
import os
from pathlib import Path
from darwinagent import Config

config = Config.from_env(work_dir=Path("runs/advanced"))
config.model_fast = "your-fast-model"
config.fast_base_url = "https://fast-provider.example/v1"
config.fast_api_key = os.environ.get("OPTIONAL_FAST_API_KEY", "")
config.role_tiers = {**config.role_tiers, "extraction": "fast", "tools": "fast"}
config.model_profiles = True
config.reasoning_effort = "low"
config.thinking_disabled_roles = {"extraction", "tools"}
```

厂商 profile 与 reasoning 参数需要对应模型／端点支持；它们在此显式启用。默认配置不添加这些厂商参数。配置好新的路由后，请创建新实验目录。
