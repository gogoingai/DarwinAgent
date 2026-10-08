# 可运行样例

[English](README.md)

在你的 Python 环境中安装 `darwinagent`，把需要的脚本保存到自己的项目，按[配置指南](../docs/zh-CN/configuration.md)在同一目录创建 `.env`，然后从该目录运行脚本。

| 脚本 | 用途 | 运行方式 |
| --- | --- | --- |
| [live_demo.py](live_demo.py) | 运行内置的两轮优化演示 | `python live_demo.py` |
| [custom_task.py](custom_task.py) | 用自己的设备记录运行问答并评分 | `python custom_task.py` |
| [custom_experiment.py](custom_experiment.py) | 用自己的设备任务运行优化与独立评测 | `python custom_experiment.py` |

三个真实模型脚本均可独立运行，只依赖安装包已声明的依赖。它们使用你配置的模型，限制为 40 次请求尝试、1800 秒。独立实验使用新输出目录。完整中文脚本和解释见[快速开始](../docs/zh-CN/quickstart.md)、[自定义任务](../docs/zh-CN/custom-tasks.md)和[自定义优化实验](../docs/zh-CN/custom-experiments.md)。

本目录的脚本使用英文记录或输出标签；中文指南提供对应的中文样例。核心安装包不包含仓库根目录的 `examples/`，普通用户可直接复制指南中的脚本，无需下载整个仓库。

[third_domain.py](third_domain.py) 是使用录制响应的离线样例。从源码目录执行 `uv run python examples/third_domain.py`，不会请求模型。
