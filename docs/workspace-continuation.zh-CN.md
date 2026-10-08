# 人工干预、续跑、Wiki 补查与跨电脑恢复

Workspace 分别保存原始内容、执行来源和分支选择。内容编号由格式与原字节决定；需要规范 JSON 时单独登记，不格式化历史文件。`KernelAssets.content_id` 不包含生成来源，旧 manifest 和旧 version 的含义不变。

分支分别记录 `working` 与 `adopted`。人工选择更新工作候选，不把它写成已证明更好的正式版本。自动发布检查分支修订号，避免旧结果覆盖新人工选择。任务失败和重试保留为不同尝试，已有响应与工具结果可以从持久化记录接续。

已使用操作者指定的 `glm-5.3-flash` 验证真实小题与原生优化循环；实际范围、评分、恢复和待完成补查见[2026-10-08 验收记录](plans/intervention-wiki-loop-acceptance-20261008.md)。离线回放与单元检查另行记录，不代表真实模型质量或全量基准收益。

## 日常操作

以下 `--root` 指实验目录，持久化数据库位于其 `workspace/` 内。除显式使用 `wiki-query --view regroup --allow-model` 外，这些命令不调用模型。

```bash
darwinagent workspace --root runs/example status
darwinagent workspace --root runs/example intervene change.json
darwinagent workspace --root runs/example fork alternative --parent main
darwinagent workspace --root runs/example preview cases.json --selection selection.json
darwinagent demo --output runs/example --preview --execution selection.json
```

人工草稿例如 `{"kind":"select","candidate":"candidate-version"}`。代码修改可用 `{"kind":"code","description":"修正分页"}` 登记，在安全边界重启。配置、目标和评测策略先保存为人工事件，由执行入口在对应边界应用。资产草稿即使有语法问题也可以保留；登记本身不等于执行准入。

预览的 `cases.json` 是对象列表，每项包含 `id` 与 `questions`；问题包含 `id`、`text` 及可选 `parameters`。`selection.json` 可包含 `mode`、`case_ids`、`question_ids`、`stages`、`branch`、`strict`、`candidate`。

五种模式分别是 `continue`、`retry_failed`、`rerun`、`fork`、`rerun_all`。阶段包括 `facts`、`vector`、`graph`、`retrieval`、`answer`、`check`、`review`、`score`、`proposal`、`candidate_check`、`wiki`、`report`。预览分别列出复用、执行和缺失输入。缺少输入不会自动授权相邻阶段或重建基线。demo 的 `--execution` JSON 可用于实际执行和预览；下列共同参数也接入 LoCoMo 与 TravelPlanner。`--strict-comparison` 显式开启冻结条件比较。

## 请求中断后的处理

模型请求先登记，再提交；完整响应有独立持久化回执。恢复先查回执，没有回执的已提交请求标为 `unknown`，不会自行重发。

```bash
# 旧执行器停止后恢复。
darwinagent workspace --root runs/example request recover --executor-stopped
darwinagent workspace --root runs/example request resolve REQUEST_ID --action response --response saved-response.json
darwinagent workspace --root runs/example request resolve REQUEST_ID --action new-attempt
darwinagent workspace --root runs/example request resolve REQUEST_ID --action abandon
```

`new-attempt` 新建待提交请求，记录可能重复费用，保留旧请求。此命令本身不发起调用。Workspace 回执不能恢复任意 Python 内存状态或半个流式响应；执行日志仍需记录下一步位置。

## 安全导出与迁移

```bash
darwinagent workspace --root runs/example export portable-bundle
darwinagent workspace --root runs/other import portable-bundle
darwinagent workspace --root runs/example export old-run-bundle --legacy-source /path/to/old-run --path-map path-map.json
```

公共导出包含数据库快照和完整实验目录中的原始进度：步骤日志、提案对话、Wiki 索引和归纳任务、图、答案及历史轮次。另带资产版本登记、完整资产依赖和独立请求回执。资产版本编号与对象编号分别解释。导入将每个登记的资产包完整恢复到独立版本目录，重定位登记路径；目标旧目录碰撞也不会混合出一套资产。文件引用使用相对路径。导出不带密钥、活跃锁或停止信号；历史控制事件仍作为记录保留。导出目录必须在实验目录之外，且不能覆盖已有目录。

导入先校验路径与摘要，再暂存和登记。操作路径如 `graph_path`、`case_path`、`journal_path` 改为新目录引用；原文件字节仍作为原件保存，不改写原执行身份。目标已有不同内容时隔离导入文件，保留目标成果。目标已有新图时，旧答案引用隔离保存的原图，不能重新解释成新图的节点。

旧目录扫描覆盖所有轮次，并递归追踪 JSON 的外部路径。`path-map.json` 将旧绝对路径映射到当前可访问文件。缺失依赖列入报告，不补造来源。通用旧目录存档不自动猜测检查点语义。已知 Pipeline 旧平面布局可通过 `register_legacy_case` 登记，但必须有实际旧 CaseInput 或保存的 `case.json`，以防同名问题正文变化后误用旧答案。没有问题版本绑定时只保留原件并报告缺口。

低层 `export_workspace` 只导出数据库已登记的对象和记录；要搬完整执行进度，应使用上述公共命令或 `export_run`。

## Wiki 原件查询与即时重新归纳

```bash
darwinagent workspace --root runs/example wiki-query '检查 q3 的分页是否前进' --scope scope.json --view raw
darwinagent workspace --root runs/example wiki-query '检查已有判断与反证' --scope scope.json --view summary
darwinagent workspace --root runs/example wiki-query '重新核对原因及反证' --scope scope.json --view regroup
```

范围可以限定问题、案例、资产和阶段。回复返回证据引用、覆盖范围、缺失内容及固定快照的 cursor。继续同一查询时传 `--cursor`；需要看到新增纠正时重新查询。

未启用 `--allow-model` 时，重新归纳保存待办任务并返回已取得原件。启用后只使用显式配置的 Wiki 模型连接，不探测或切换其他模型。默认实际 HTTP 尝试上限 80、执行时间 900 秒，可通过 `--max-requests` 与 `--timeout` 调整。

查询只访问登记允许的训练证据、资产、纠正和聚合信息。标准答案、原始 judge 请求及验证／测试逐题轨迹不能通过此接口读取。人工纠正也必须引用允许的来源。原件缺失仍显示缺失，部分块完成不能表述为已检查全部范围。


提案输入中每道题提供 `wiki_scope`，可以直接用于查询；其中 `training_ids` 精确标识 case 与题号的配对。同名局部 `question_ids` 应同时限定 `case_ids`。为兼容旧会话，也支持在 `question_ids` 中填写完整组合训练编号。重新归纳没有匹配到可读原件时返回 `partial` 和缺口，不声称已完成模型归纳。

Wiki 维护有独立恢复入口：

```bash
darwinagent workspace --root runs/example wiki-maintenance-retry EVENT_ID --reason '明确重试中断的归因'
```

该命令只登记新尝试，不调用模型，保留旧请求和可能重复费用。使用已有配置的维护器调用 `await wiki.resume_maintenance(EVENT_ID)` 才执行；省略编号可补做待归因任务。整轮 outbox 已完成也不会隐藏归因待办。可靠保存的回应直接复用，未知提交仍需显式选择如何恢复。daily 模式的候选冒烟结果和题内步骤保存在 `smoke/<资产版本>/`。

## 领域入口与实际阶段选择

```bash
darwinagent demo --mode replay --rounds 0 --output runs/demo-scoped --execution-stages graph,retrieval,answer,check,review,score
darwinagent demo --mode replay --rounds 0 --output runs/demo-scoped --resume --execution-mode rerun --execution-stages score
python -m datasets.locomo.run --output runs/locomo-small --case conv-26 --preview --execution-question-ids 0,15,23 --execution-stages answer,check
python -m datasets.travelplanner.run --output runs/travel-small --split train --index 0 --preview --execution-stages score
```

共同参数为 `--execution-mode`、`--execution-stages`、`--execution-question-ids`、`--execution-branch`、`--strict-comparison`、`--preview`。普通执行和训练续跑默认采用 daily 选择。持出集 campaign 保留独立冻结协议；局部优化使用训练路径，不静默改变验证／测试的覆盖范围。

LoCoMo 的评分标准编号依据实际评分锁、受锁源码和判题参考内容；缺失审计参考时不声称可靠复用。TravelPlanner 依据实际官方规则、数据库参考、题目与评测桥。自定义评测器没有显式标准编号时，不能据此宣称评分可可靠复用。仅改 judge 连接不改标准编号；显式只重评分使用新 judge，不生成新答案。

同一待归纳／失败查询的证据和归纳进度没有变化时，提案会话暂停。先修复或显式重试 Wiki 任务，再调用 `session.retry_wiki(reason)` 并继续 `run(...)`；原查询重新检查，已有对话和归纳结果保留。
