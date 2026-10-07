# 分层 Wiki、动态补查与离线五题冒烟

Wiki 将完整训练原文、题目、实际答案、完整工具轨迹和资产内容先保存为不可变对象，再构建展示视图。内容同时登记到工作空间 SQLite 的出处和引用表；`optimization/evidence` 保存查询索引、固定快照、回复和归纳任务。旧摘要缺失原件时不会补造历史。

`WikiMaintainer.record` 会摘出 `_original_training_evidence`，按 case/question 登记完整原件。展示 facts 中保存 `evidence_refs`；归纳事件的稳定编号不包含展示引用，因此重放不会重复累计同一经验。人工纠正通过 `service.correct(target_ref, text, source_refs=...)` 新增反证，不覆盖原判断。下一次查询会看到目标对象及纠正；旧 cursor 继续读取旧快照。

分页 offset/cursor/limit、下一位置、是否还有数据、返回/匹配/扫描计数及截断原因在展示中保持结构化。程序发现未推进或空页后还有数据时保存待核对线索和原件位置；线索不是原因结论，需要检查过滤扫描契约。靠后的无执行报错异常优先进入初始上下文。

## 查询接口

```python
from darwinagent.experiments.wiki_service import WikiQuery

reply = await wiki.service.query(WikiQuery(
    question="检查 q3 跨页是否推进，归纳支持与反证",
    scope={"case_ids": ["smoke-case"], "question_ids": ["q3"]},
    view="regroup",  # raw / summary / regroup
    max_chars=8000,
))
data = reply.to_dict()
```

`raw` 返回完整原件的片段；`summary` 返回可定位原件的简短视图；`regroup` 读取原件，使用已有的 `wiki_maintainer` 连接重新归纳。没有配置连接时返回原件并保存 pending 任务，不寻找替代模型。

回复包含 status、证据快照版本、原件引用、事实、假设、支持、反证、不确定性、覆盖、缺口、cursor 和任务编号。大对象使用 `raw_fragment` 或 `regroup_fragment`，连续 offset 片段拼接后得到 JSON。若引用和覆盖本身太大，使用 `reply_fragment` 将完整回复也分页；其拼接结果是一个完整 `WikiReply.to_dict()`，其中可包含继续读取原件的 cursor。每页按实际 JSON 序列化字符数检查，最多 8,000 字符或更小的请求预算。cursor 绑定查询和不可变快照，不能用于其他问题。

重新归纳分块保存 request、实际 response、完成状态和 StepJournal。每块原件不超过 9,000 字符，给完整 JSON 输入和协议留出余量。各块结果完成后按二叉层级合并，保存每个合并组，保留支持、反证和未解释问题。每组输入实际序列化不超过24,000字符、归纳结果不超过4,000字符；超过预算时明确保留 `global_merge` 缺口，不把单块结论称为全范围结论。已完成原件块和合并组都不会重复调用。

明确失败或未知提交不自动重发。操作者确认重试后调用 `service.retry_job(job_id, chunks=[1])`；只为未完成的选定块建立新尝试。全局合并失败可使用 `retry_merge=True`；旧尝试仍在。补充预算不会清零历史完成块。

## 权限与来源

允许来源类型为 training、asset、correction 和 allowlisted aggregate。聚合对象只接受 metrics、total、completed、故障计数、口径与分组等统计字段，禁止逐题答案、标准答案和 judge 请求。所有来源引用都必须指向已登记的允许对象；未知引用拒绝。纠正同样接受来源检查。回答 Agent 不直接读取优化 Wiki。

接入方必须按真实来源类型登记对象，不能把验证/测试逐题数据伪装成训练对象。服务对嵌套 gold、expected_answer、standard_answer、judge_request 等字段作额外拒绝检查；这不是语义识别器，无法从任意文字辨别用户谎报的来源。

## 五题离线回放

```sh
PYTHONPATH=src python scripts/smoke_intervention.py --mode replay --output /tmp/darwin-five-smoke
```

脚本使用公共 Pipeline、预置原文和事实图、登记资产及独立评测器，覆盖事实、日期、130 行跨页、多个工具结果和合法拒答。成功后改变并发并继续，检查新增回答调用为零；同一次提案先要求 Wiki 重新归纳 q3 原件，再继续输出 no_change。报告、提案对话、工具轨迹、Wiki 请求和归纳结果均写入输出目录。

这只是机制的离线回放：HTTP 尝试为零，不能代表真实模型质量或分数提升。真实入口要求操作者明确指定模型后运行：

```sh
DARWINAGENT_BASE_URL=<用户指定连接> DARWINAGENT_API_KEY=<环境密钥> \
  PYTHONPATH=src python scripts/smoke_intervention.py --mode live \
  --model '<用户指定模型名>' --output /tmp/darwin-live-five-smoke
```

`--model` 必填，所有生成、检查、提案和 Wiki 角色都使用这个模型，不读取环境默认模型来替代它；不会探测其他模型或调用嵌入端点。并发 1、共享最多 80 次实际 HTTP 尝试、900 秒主动时间，重试也计入共享上限。第一步动态提案协议要求对 q3 发起 `regroup`，之后仍在同一会话继续推理。真实与回放输出目录不能混用；事实图和原文准备逻辑共享，但执行来源、请求、回执和答案分别保存。

原答案复用时 `RunResult.answer_provenance` 指向真正生成答案的原始 case、图、资产和请求来源。Wiki 从这些来源构建原件，不把旧答案与人工修改后的新原文拼接；旧来源缺失标未知。归纳请求已接入工作空间请求台账，预算不足、未知结果和明确放弃分别保留为待办状态。raw 回复的 `matched` 是查询找到的集合，`covered` 只描述本页实际返回的片段范围，不能把分页首段当作全部覆盖。

本次已使用用户指定的 `glm-5.3-flash` 完成五题生成和三个额外 case（六题）。动态归纳的最终状态、真实失败反例及独立校验记录见 [验收记录](plans/intervention-wiki-progress.md)。离线回放不能替代真实验收，也不能证明全量评测或优化收益。

Wiki 模型输出预算可以通过 `RunConfig(wiki_max_tokens=12000)` 单独设置，避免推理耗尽正文预算；其他角色的预算保持独立。分块条目应使用 `{"text":"事实","ref":"原件编号"}`，合并条目使用 `{"text":"结论","evidence_refs":["原件编号"]}`。字段错误会返回具体格式和允许的引用；任意不存在的引用仍被拒绝。

提案中的无效 cursor 会作为已保存的失败回复返回同一会话，并附带原查询修正提示。继续 cursor 时必须保持 question、scope、view 不变；新问题使用 null。`continuation_query` 给出可直接继续的完整查询。

真实脚本支持 `--active-seconds`、`--max-requests`、`--wiki-max-tokens` 及显式的 `--retry-failed-wiki`。增加预算继续累计原消耗；失败重试保留旧回执，不自动处理结果未知的请求。额外 case 可使用 `scripts/smoke_extended.py --model <指定模型> --output <独立目录>`，包含主体日期、历史归属、缺失记录、75 行跨页及响应保存后的中断恢复。

大归纳回复优先展示完整合并结论，随后才是分块细节。`--refresh-wiki-reply` 显式把保存的完成结果送回同一提案会话，不重复 Wiki 模型调用；旧 cursor 保持原顺序和快照。
