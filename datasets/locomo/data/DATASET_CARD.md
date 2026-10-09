# 中文 LoCoMo 原始数据来源

原始文件由 [justis-xu/memory-eval-zh 的 locomo 目录](https://huggingface.co/datasets/justis-xu/memory-eval-zh/tree/main/locomo) 托管，数据说明和许可见该仓库的 [README](https://huggingface.co/datasets/justis-xu/memory-eval-zh/blob/main/locomo/README.md)。

DarwinAgent 的 Git 仓库保留接入代码、文档和小型测试夹具。运行时通过 `--dataset-repo` 和 `--dataset-revision` 选取数据，程序记录实际提交与文件校验值；缓存位置可选。本地数据仅通过 `--data-dir` 显式选择。

构图输入只读取 `locomo10_zh.json` 的消息正文、说话人与会话日期。英文 `locomo10.json` 供独立判题核对；QA 派生事实、槽位、历史图和实验答案不会自动加载。
