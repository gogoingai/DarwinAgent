# 迁移

[English](../en/migration.md) · [简体中文](../zh-CN/migration.md) · [README](../../README.zh-CN.md)

DarwinAgent 0.1.0 是新的包身份；此前历史框架版本不能按版本号直接比较。当前发行名和导入名均为 `darwinagent`，没有旧命名空间兼容 shim。

1. 从当前源码检出安装，使用 `from darwinagent import ...`；不要仅修改旧冻结运行的 imports。
2. 通用模型连接改为 `DARWINAGENT_API_KEY`、`DARWINAGENT_BASE_URL`、`DARWINAGENT_MODEL`。历史数据集配置在数据集边界显式加载。
3. 创建新的输出目录，重新冻结源码／资产／模型／评测身份。旧运行只能用原始源码与依赖继续，不可重算旧锁绕过校验。
4. 第三方任务实现 `DatasetAdapter` 与 `Evaluator`，登记 S/F/C/P，使用共同 `Pipeline`；不存在任意任务执行回调或外部 Agent 插件入口。
5. 核心 wheel 不携带根目录数据集、历史输出、测试或第三方环境；演示资源已经内置。

[历史索引](../history/README.md)保留此前文档和研究输出，历史脚本保持原字节，可能需要原始检出版本。当前入口是[快速开始](quickstart.md)；旧文档不作为当前 API 说明。
