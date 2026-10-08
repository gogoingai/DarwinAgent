# 参与 DarwinAgent

[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md)

DarwinAgent 0.1.1 是实验版本。欢迎提交可复现的问题、独立任务示例、文档改进和受控评测研究。公开讨论请使用 [GitHub Issues](https://github.com/gogoingai/DarwinAgent/issues)，不要提交凭据或私有输入数据。

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
