# 参与 DarwinAgent

[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md)

DarwinAgent 是实验版本。欢迎提交可复现的问题、独立任务示例、文档改进和受控评测研究。公开讨论请使用 [GitHub Issues](https://github.com/gogoingai/DarwinAgent/issues)，不要提交凭据或私有输入数据。

## 版本与兼容性

版本号采用 `A.B.C`。除重大架构调整外，常规发布只递增末位 `C`，并保持向后兼容；这也适用于新增功能。当前已发布的 `0.2.0` 保留，下一次常规发布为 `0.2.1`。

- 修复、功能增加、性能改进、默认行为或配置调整、数据输入方式变化，都不能单独作为升级 `A` 或 `B` 的理由。改动数量也不决定版本跨度。
- 保持公开 Python API、CLI、配置及持久化产物的兼容性，包括已有运行的恢复能力。新增字段提供兼容默认值；格式变化提供兼容读取或迁移工具，并验证旧用法仍然可用。
- 重大架构调整或无法兼容的改动，先说明影响、迁移方案和版本选择理由，获得项目维护者明确确认后再实施或发布。必要时先通过兼容层和弃用周期完成过渡。
- 普通提交不自动发版。准备发布时同步包元数据、CLI、[更新历史](CHANGELOG.md)和当前文档；README 图片不写版本号，版本徽章保持动态。

自动化开发同样遵守根目录 [AGENTS.md](AGENTS.md)。发布流程见 [PyPI 发布维护](docs/zh-CN/pypi-publishing.md)。

## 准备与验证

```bash
uv sync --frozen --group test
uv run --frozen --group test ruff check .
uv run --frozen --group test ruff format --check .
uv run --frozen --group test python -m unittest discover -s tests -v
uv run --frozen --group test python -m unittest discover -s datasets/locomo/tests -v
uv run --frozen --group test python -m unittest discover -s datasets/travelplanner/tests -v
uv run --frozen --group test python examples/third_domain.py
uv build --wheel
```

运行与改动相关的检查，报告实际结果和缺失依赖。涉及安装包时，还应使用 [scripts/check_installed.py](scripts/check_installed.py) 在源码目录外验证 wheel。当前完整测试有七项需要历史／官方／形式验证资源的显式跳过，详见[验收记录](docs/acceptance/2026-10-07.md)。

## 设计边界

统一使用 Ruff 0.16.10 做格式和静态检查，目标为 Python 3.11、100 字符行宽。
格式化命令是 `uv run --frozen --group test ruff format .`。冻结 S/F/C/P 资源、
历史脚本、数据和独立基线不参与自动格式化，它们的字节属于证据身份。
继续提交 `uv.lock`，新增工具时避免顺带升级已有依赖。

录制客户端、图和产物构造器放在 `tests/support`，其中不能定义 TestCase 或导入
测试场景文件。测试从生产辅助函数实际所属模块导入；新场景按行为组织，移动时
保留原断言。详见[模块职责](docs/zh-CN/architecture.md)和
[工程治理报告](docs/zh-CN/engineering-governance.md)。

- 内核不能依赖仓库数据集或测试模块；生成输入与评测参考必须分开。
- S/F/C/P 修改要遵守固定执行能力、检查和评测契约。不能为了采纳候选而放宽规则。
- 保留历史报告、数据、诊断脚本与冻结身份。补充新证据，不覆盖旧成绩，不重算旧锁。
- 源码、模型或配置变化后使用新目录。真实模型检查需要显式端点及请求／时间限制；常规检查应离线运行。
- 中英文当前文档保持一致，新图应附可编辑图源。

## 提交 PR

说明问题、修改后的行为和验证结果；区分测量事实、假设与限制。保持改动聚焦，注明外部依赖、来源与 API 影响。保留第三方版权和许可证信息。贡献按项目 MIT 许可证提交；第三方材料仍遵守各自条款。

讨论应具体、尊重彼此，并依据证据。项目目前使用公开 Issue 沟通，没有声明私有报告地址。
