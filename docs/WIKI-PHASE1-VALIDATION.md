# Wiki 首期缩量验收记录

日期：2026-10-05。实现分支：`wiki-experience-phase-one-20261005`，worktree：`/Users/xu/git/oak/.cache/worktrees/wiki-experience-phase-one`。主工作树及此前的运行目录不变；本次只使用 `conv-26` 前 10 道训练题，最多 10 轮，`--scope sfcp`。不揭盲验证/测试集。

## 离线与恢复门

- 框架测试：`PYTHONPATH=. /Users/xu/git/oak/.venv/bin/python -m unittest discover -s tests -q`，259 项通过，2 项既有跳过。
- LoCoMo 协议测试：`PYTHONPATH=. /Users/xu/git/oak/.venv/bin/python -m unittest discover -s datasets/locomo/tests -q`，12 项通过。
- `tests/integration/test_wiki_optimization.py`：14 项通过。包含录制端 10 轮持续使用 Wiki、拒绝候选记录、B0 10 次纠错上限、B0 失败试跑回收、维护额度用尽后事实保留、提案和 decision 落盘后恢复、准入报告身份与冒烟状态验证、同一尝试联合修改 S/F/C/P，以及对全部独立试跑错误的去重反馈与遍历场景名称保留。
- `git diff --check` 通过。

## 真实缩量运行

### 首次运行：失败，保留冻结

目录：`datasets/locomo/runs/wiki_10q_10r_20261005_112306`。`precheck.json` 显示模型档位、嵌入、快照和检索冒烟均通过；`train/experiment.json` 声明训练 `conv-26`、10 轮上限、S/F/C/P 和 Wiki 维护模型上限 30 次，训练问题指纹核对为前 10 道题。

`train/B0/bootstrap-call.json` 显示冷启动 5 次模型尝试，3 次候选试跑不通过，2 次 JSON 格式错误；3 份不通过的真实图报告见 `train/B0/bootstrap-trials/`。失败类型包括 F 输出字段契约不匹配、遍历条件不合法。`train/failure.json` 记录最终 `ProtocolError`；**未产生 B0 资产或正式评分，完成轮数为 0**。这次运行不再恢复。首轮暴露出 B0 终态失败时 Wiki 未消费试跑报告的问题，已在代码中修复并补充专项测试。

### 第二次运行：主动停止，保留冻结

目录：`datasets/locomo/runs/wiki_10q_10r_20261005_retry1`。新代码预检全部通过；Wiki 模式 B0 纠错上限由 5 次增为 10 次，**所有静态、压力、历史回放、冒烟及正式评分门槛不变**。在 5 份 B0 真实图报告均显示 29 个必需场景失败后，得到用户同意主动终止，**未产生 B0 正式评分、完成轮数为 0**。5 份报告按资产和错误去重后约 6 类独立错误；原 `AdmissionError` 只反馈前 12 条失败，模型反复逐字段修补。新代码仅在 Wiki B0 的协议反馈中列出全部独立错误和重复次数；用上述报告验证生成约 720 字符的去重摘要。被停止的运行目录不再恢复。

### 第三次运行：B0 失败，保留冻结

目录：`datasets/locomo/runs/wiki_10q_10r_20261005_retry2`。新实验身份的 `precheck.json` 已通过，`train/experiment.json` 声明同一组前 10 道题、最多 10 轮、S/F/C/P 和 Wiki 维护上限 30。运行期间代码保持冻结。`train/B0/bootstrap-call.json` 记录 10 次模型纠错输出，其中 9 次真实图试跑拒绝、1 次未进入试跑；最后一份试跑仍因 `f_relation_expand` 的 `high_degree_out` 没有可生成的合法输入触发能力路径而拒绝。早期报告另有 `high_degree_in`、非法遍历输入等失败。`train/failure.json` 记录最终 `ProtocolError`；`train/optimization/wiki.json` 记录 9 条试跑事实和 1 条终态失败，维护额度预留 1/30。**没有 B0 资产或正式评分，也没有 R 轮 decision，真实完成轮数为 0**。这次运行不再恢复。

终态暴露的反馈盲点：原去重摘要只保留资产、状态和错误文本，丢失 `scenario_id`，模型无法区分需要补足哪个高出入度压力场景。已在后续代码中让去重反馈保留场景名称，并针对遍历压力输入补充可执行的参数提示，保持去重、准入判定与纠错次数不变；修复不反向修改本次冻结的运行证据。后续若重新运行，须以新的 `train/B0/stage.json`、`train/R*/decision.json`、`train/optimization/wiki.json` 和 `train/summary.json` 实际落盘为准，不得用启动状态代替十轮验收。

## 判读边界

前 10 题日期类占多数；该缩量验证只能检验训练经验闭环和运行准入，不能推断过滤/遍历类题型的完整收益、全量召回或图/向量对照结论。若 B0 或候选准入失败，应记为质量门拦截，不能把未产生的轮次写成已完成。
