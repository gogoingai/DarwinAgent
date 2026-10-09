# 迁移

[English](../en/migration.md) · [返回中文首页](../../README.zh-CN.md)

## 0.3.0 准备版本：显式接入通用 Agent

0.3.0 是本地准备、尚未发布的版本，通用运行时架构已获得明确授权。
普通兼容更新仍只增加末位版本号。本版本从当前源码或本地 wheel 安装；核验时公开 PyPI
版本仍为 0.2.0。

- 原有 `Pipeline`、默认 `ExperimentRunner`/`CampaignController`、CLI 和 v3 资产包继续
  使用图问答路径。不会批量重写 v3 为 v4；旧 manifest、资产字节、指纹、历史与评测锁保持不变。
- 新 Agent 任务使用 `AgentTask`、`ExecutionBackend`、固定 `ExecutionProfile` 与 v4
  `AgentKernelBundle`。实验通过显式 `backend=` 选择新路径，不能直接转换旧 QA 检查点。
  完整接入见 [SDK 与示例](agent-sdk.md)。
- 普通适配器类声明 `identity()`；已知读取失败捕获结构化 `ToolFailure`。
  写入或模型请求已提交但回执丢失时仍为 `unknown`。
- 必须提供明确 run ID；写工具按 episode 创建工作区。源码、模型、数据、工具或评测身份
  变化后新建运行目录，保留旧目录。旧冻结运行使用原来的源码与依赖恢复，不能改写旧身份。
- 核心包支持 Python 3.11+。可选原生 LIFT 集成从源码目录、固定 Python 3.12 环境运行，
  不增加核心包的无条件依赖或导入。

## 历史包迁移：0.1.0

DarwinAgent 0.1.0 是新的包身份；此前历史框架版本不能按版本号直接比较。当前发行名和导入名均为 `darwinagent`，不提供旧命名空间的兼容层。

1. 使用 `python -m pip install darwinagent` 安装，或检出当前源码用于开发，使用 `from darwinagent import ...`；不要仅修改旧冻结运行的导入语句。
2. 通用模型连接改为 `DARWINAGENT_API_KEY`、`DARWINAGENT_BASE_URL`、`DARWINAGENT_MODEL`。历史数据集配置在数据集边界显式加载。
3. 创建新的输出目录，重新冻结源码／资产／模型／评测身份。旧运行只能用原始源码与依赖继续，不可重算旧锁绕过校验。
4. 第三方任务实现 `DatasetAdapter` 与 `Evaluator`，登记 S/F/C/P，使用共同 `Pipeline`；该历史版本没有外部 Agent 插件入口，准备中的 0.3.0 已通过上方 SDK 增加此路径。
5. 核心安装包不携带根目录数据集、历史输出、测试或第三方环境；演示资源已经内置。

[历史索引](../history/README.md)保留此前文档和研究输出，历史脚本保持原字节，可能需要原始检出版本。当前入口是[快速开始](quickstart.md)；旧文档不作为当前 API 说明。

## 2026-10-07 工程治理的历史迁移记录

当时包版本为 0.1.0，移动实现或格式化会改变源码身份。该次迁移使用新的输出目录；
相同新源码可以续跑，旧源码检查点必须明确拒绝，不能改写原声明来绕过校验。

当时 LoCoMo 使用 `evaluation_lock.governance-20261007.json`，覆盖原四个文件，
另加 `pipeline/dates.py` 和共享的 `operators/calendar.py`。历史 `evaluation_lock.json`
保持原字节，校验新源码时应失败。显式 `lock_path` 参数继续可用，但需配合其对应源码。
新锁记录源码身份，不代表新成绩或评分规则变化。详见[来源与验收](engineering-governance.md)。


## 2026-10-09 显式 HF 数据源

当前 LoCoMo 运行要求命令指定 HF 仓库与版本，或显式选择本地输入。实际 HF 提交和原始文件校验值独立于缓存位置记录。当前评测接口使用 `evaluation_lock.governance-20261009.json`，此前锁文件保留；默认使用原始 QA，修订参考须显式传入。
