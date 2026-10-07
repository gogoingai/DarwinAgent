# 迁移

[English](../en/migration.md) · [简体中文](../zh-CN/migration.md) · [README](../../README.zh-CN.md)

DarwinAgent 0.1.0 是新的包身份；此前历史框架版本不能按版本号直接比较。当前发行名和导入名均为 `darwinagent`，没有旧命名空间兼容 shim。

1. 从当前源码检出安装，使用 `from darwinagent import ...`；不要仅修改旧冻结运行的 imports。
2. 通用模型连接改为 `DARWINAGENT_API_KEY`、`DARWINAGENT_BASE_URL`、`DARWINAGENT_MODEL`。历史数据集配置在数据集边界显式加载。
3. 创建新的输出目录，重新冻结源码／资产／模型／评测身份。旧运行只能用原始源码与依赖继续，不可重算旧锁绕过校验。
4. 第三方任务实现 `DatasetAdapter` 与 `Evaluator`，登记 S/F/C/P，使用共同 `Pipeline`；不存在任意任务执行回调或外部 Agent 插件入口。
5. 核心 wheel 不携带根目录数据集、历史输出、测试或第三方环境；演示资源已经内置。

[历史索引](../history/README.md)保留此前文档和研究输出，历史脚本保持原字节，可能需要原始检出版本。当前入口是[快速开始](quickstart.md)；旧文档不作为当前 API 说明。

## 工程治理后的源码身份

包版本仍为 0.1.0，移动实现或格式化都会改变源码身份。本次检出使用新的输出目录；
相同新源码可以续跑，旧源码检查点必须明确拒绝，不能改写原声明来绕过校验。

LoCoMo 默认使用 `evaluation_lock.governance-20261007.json`，覆盖原四个文件，
另加 `pipeline/dates.py` 和共享的 `operators/calendar.py`。历史 `evaluation_lock.json`
保持原字节，校验新源码时应失败。显式 `lock_path` 参数继续可用，但需配合其对应源码。
新锁记录源码身份，不代表新成绩或评分规则变化。详见[来源与验收](engineering-governance.md)。
