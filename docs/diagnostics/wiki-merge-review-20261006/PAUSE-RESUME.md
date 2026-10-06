# 限流暂停后的恢复（2026-10-06，保留现有结果）

恢复位置是**阶段六外测**。阶段五 loop10 已有 R1–R10 决策及汇总；loop12 与 loop_final 是后来中止的重启，不继续它们。本次没有恢复模型任务，也没有移动真实检查点。

## 离线核对

已核对六组的完整运行身份、源码、配置、模型路由、锁定资产及快照；按当前投影重新建出的图与保存图逐组一致。两臂均使用图重建、K=30；G1 记录并发为 8，V0 为 4。恢复时保持这些参数，使用进程串行调度，不直接改 RunConfig 的并发值触发身份变化。

锁定资产：`wiki_main_repaired_loop10_20261006_081956/train/published/versions/84957c8f4e854c2da7c2dcb00143b47eb44eb1dccaa89d3d83bc4a35ee3be062`。

| 臂／对话 | 已保存／总题 | 未生成 | 已保存的 429 故障 | 其他故障 |
|---|---:|---:|---:|---:|
| V0 / conv-42 | 260/260 | 0 | 3 | 5 |
| V0 / conv-43 | 242/242 | 0 | 2 | 7 |
| V0 / conv-50 | 10/204 | 194 | 1 | 0 |
| G1 / conv-42 | 236/260 | 24 | 13 | 3 |
| G1 / conv-43 | 152/242 | 90 | 11 | 4 |
| G1 / conv-50 | 15/204 | 189 | 2 | 0 |

共 915 条答案检查点：864 条正常回答或拒答、32 条 429、19 条其他执行故障。需补 497 条缺失答案＋32 条限流故障，已有有效回答不重答。另有 267 条成功判分缓存，必须复用。详单及身份摘要在 `resume-plan.json`。

当前 `graph_rules.py` 和 `external_test.py` 有其他 agent 留下的未提交修复；不回滚、不覆盖。此次离线核对确认其当前行为与已有保存图一致，恢复前再次检查即可。

## 需要修正的结果口径

1. 终报的“8 个完整轮”包含 R4、R8 的不完整评分。真实情况为：10 次尝试，6 个训练评分完整且零生成/评测故障，2 个评分有故障，2 个未完成正式评分。R5 的验证仍有故障，不能据此宣布整个十轮验证通过。
2. R2 三个尝试分别是：非训练证据引用、两次冒烟的候选发布失败；不能全归因到 429。R3 的在途提案曾出现 429，恢复后记超时。`round_elapsed_s` 按原始开始时间计算，包含中断时间，不能单凭它超过 900 秒认定执行熔断失效。
3. R5-val 是 1 个**生成故障**，评测故障为 0；原文所写“评测故障”应改正。
4. loop10 的 B0 训练有 2 个故障，验证有 4 个故障。R6 训练 13/15、验证 6/10、两侧零故障，确实被采纳；相对故障基线的增分混有运行恢复收益，不能直接等同于稳定答题质量提升。
5. 外测 19 个非限流故障包括：10 个答案证据构造失败、8 个协议/审查耗尽、1 个工具参数越过契约。先保留并独立归因，不按 429 清除，更不能据测试错题修改锁定 F/C/P 再混报成绩。
6. 当前 V0 根目录的 report.json 只含 conv-42，尚非三对话终报。恢复结束后应汇总全部三段。图是否改变与 Wiki 是否有效仍分别报告。

## 恢复操作

**只有用户结束暂停、限流窗口恢复后才执行以下模型任务。**先确认没有残留外测进程；一次只运行一个臂的一个进程，由同一进程依次处理三个对话。不要并行启动六组任务，不重跑训练、冷启动或真实模型预检。

### 1. 再次只读核对，再准备一次限流补答

```bash
cd /Users/xu/git/oak
export PYTHONPATH=.
.venv/bin/python docs/diagnostics/wiki-merge-review-20261006/prepare_external_resume.py
```

上一步必须成功。若身份/源码/资产/图不一致，先核对原因，禁止改 identity.json、手写 CARRIED.json 或重建整个目录绕过检查。限流解除后执行：

```bash
.venv/bin/python docs/diagnostics/wiki-merge-review-20261006/prepare_external_resume.py --apply
```

此操作只把核实的 32 条 `execution_error`＋`TransportExhausted`＋429 检查点移到备份；同时备份旧报告与结果。成功答案、非限流故障、图、记忆、向量和判分缓存都不删除。`resume-applied.json` 持久记录准备状态，重复调用不会再次清掉后来产生的故障。临时目录验证已确认：仅移动指定失败、保留成功答案和判分、备份旧报告、重复准备无修改。

### 2. 先续 V0，完成后才续 G1

```bash
OAK_LOCKED_ASSETS=/Users/xu/git/oak/datasets/locomo/runs/wiki_main_repaired_loop10_20261006_081956/train/published/versions/84957c8f4e854c2da7c2dcb00143b47eb44eb1dccaa89d3d83bc4a35ee3be062
.venv/bin/python -m datasets.locomo.scripts.external_test \
  --arm v0 --assets "$OAK_LOCKED_ASSETS" --cases conv-42,conv-43,conv-50 \
  --output datasets/locomo/runs/ext_v0_final_20261006 \
  --vector-k 30 --graph-rebuild \
  >> docs/diagnostics/ext-v0-resume.log 2>&1
```

确认 V0 正常结束且 report.json 覆盖全部三个对话，再执行：

```bash
.venv/bin/python -m datasets.locomo.scripts.external_test \
  --arm g1 --assets "$OAK_LOCKED_ASSETS" --cases conv-42,conv-43,conv-50 \
  --output datasets/locomo/runs/ext_g1_final_20261006 \
  --vector-k 30 --graph-rebuild \
  --baseline datasets/locomo/runs/ext_v0_final_20261006 \
  >> docs/diagnostics/ext-g1-resume.log 2>&1
```

外测入口没有 `--resume` 参数；Pipeline 会按同身份自动复用答案与图，判题器按题目/答案/来源/模型键复用已成功判分。只生成缺失项与已归档的限流项，只判新增或变化的答案。不得新建另一组输出目录导致缓存失效。

若单进程仍持续 429，立刻停止对应运行并保留现场，再等待恢复；不同时起其他臂，不无限补答，不扩大重试次数。其他故障如实入分，独立诊断。若确认需要程序修复，另记受影响题的补验，保留锁定版本原结果，不把修复结果混成原始外测成绩，不回流训练 Wiki。

## 结束交付

分别保留暂停前与恢复后结果、补答清单、调用与成本增量，标注“限流恢复”及仍存在的非传输故障。汇总三对话两臂正确率、故障率、宏平均及有效对比条件，并按上面的口径修正 FINAL-REPORT.md。不能因训练 summary 为 failed 重启 B0，也不能宣称十个零故障完整轮已经达成。
