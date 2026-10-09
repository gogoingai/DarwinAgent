# DarwinAgent 介绍图提示与风格记录

采用用户指定的[改进循环参考图](https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png)作为全项目视觉基准。八张当前配图均为本地 PNG；中文循环原图直接复用，其余七张通过 Codex 内置图片生成工具逐张生成。图片不写版本号。旧 SVG / Mermaid 保留为结构参考，不能重建手绘 PNG。

## hero-zh-CN

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: hero-zh-CN.png
---
# 画图提示：hero-zh-CN

参考图只作为视觉风格参考。沿用它的白底、自然黑色手绘线条、浅蓝浅绿浅黄浅紫浅橙色卡片、清晰手写感标签与简洁图标。整体横向宽屏，留白充足，适合 GitHub README 和中文技术文章手机阅读。文字准确完整，不添加版本号、分数、增长曲线、水印或无关装饰。创作 DarwinAgent 项目头图。上方居中手绘圆角横幅，主标题「DarwinAgent」，标题下方「Agent 时代的进化」，副标题「让 Agent 从经验中改进」。中间以一个正在阅读经验笔记的友好简洁机器人为主视觉，左右辅以文档、工具齿轮、带勾的检查板与经验书本，形成开放、轻盈的构图。下方四张小卡片横向排列，短标签分别是「提出候选」「独立评测」「保留版本」「积累经验」，卡片之间以细小单向箭头依次连接，表达过程而非必然提升。底部短注「经验驱动的递归自改进框架」。所有文字简体中文，只有 DarwinAgent、Agent 保留英文。不要把参考图的完整流程搬进头图，突出项目名称与机器人主视觉。
```

## hero-en

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: hero-en.png
supporting_reference: hero-zh-CN.png
---
# 画图提示：hero-en

Translate image 2 (the Chinese DarwinAgent hero banner) into English, using image 1 only as the original project style reference. Preserve image 2's composition, robot, title frame, surrounding document/checklist/gear/books, four lower cards and all three arrows. Replace every Chinese label with the exact English below. Large title stays "DarwinAgent"; tagline inside frame "Evolution for the Agent Era"; subtitle "Help agents improve from experience"; robot's open book "Experience notes"; small stacked book labels "Experience" and "Knowledge". Lower four cards left to right "Propose", "Evaluate", "Retain", "Learn". Footer "An experience-driven self-improvement framework". Keep the white background, soft pastel blue/yellow/green/orange accents, black hand-drawn lines and readable friendly hand-lettered text. Widen label areas slightly if necessary. No Chinese remains; no additional text, versions, scores, curves or watermark.
```

## evolution-loop-en

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: evolution-loop-en.png
---
# 画图提示：evolution-loop-en

Translate this diagram into English. Preserve the reference image's exact visual style, white background, hand-drawn black outlines, pastel accents, icons, card positions and ALL arrow connections. Only change the labels and widen cards slightly if needed for English. Title: "DarwinAgent's Improvement Loop". Left-to-right cards: "Task feedback", "Propose a candidate", "Execute & evaluate". Small text under the checklist in the third card: "Compare with current version". Diamond: "Meets adoption rules?". Upper branch label: "Yes" and green outcome card: "Retain new version". Lower branch label: "No" and orange outcome card: "Keep current version". Both outcome cards have distinct direct arrows into the bottom large lavender card titled "Experience Wiki" with subtitle "Keep execution, scores & decisions". Curved arrow from Experience Wiki back to Propose a candidate labeled "Inform the next proposal". Footer: "Evaluation and adoption rules stay fixed; gains are not guaranteed". All text must be English and legible. Keep the two explicit outcome-to-Wiki arrows and the Wiki-to-proposal arrow unambiguous.
```

## architecture-zh-CN

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: architecture-zh-CN.png
---
# 画图提示：architecture-zh-CN

参考图只作为视觉风格参考。沿用它的白底、自然黑色手绘线条、浅蓝浅绿浅黄浅紫浅橙色卡片、清晰手写感标签与简洁图标。整体横向宽屏，留白充足，适合 GitHub README 和中文技术文章手机阅读。文字准确完整，不添加版本号、分数、增长曲线、水印或无关装饰。创作中文应用架构总览，标题「DarwinAgent 的运行架构」。用五张清晰卡片和下方一条共用能力带，不画类继承关系。左卡「任务输入」，标识 DatasetAdapter，副标「记录、问题与来源」。中间浅蓝大卡「共同运行时」，标识 Pipeline，内部唯一顺序小流程「抽取事实」→「来源图」→「查询与回答」，副标「ExtractionAgent · AnswerAgent」。右卡「独立评测」，标识 Evaluator，副标「参考答案与评分」，在卡片里加小锁图标。中下卡「实验控制」，标识 ExperimentRunner，副标「提案、准入与采纳」。右下浅紫卡「持久化记录」，副标「资产版本、评分、决策与经验 Wiki」。箭头必须如下：任务输入→共同运行时标「生成输入」；共同运行时→独立评测标「任务输出」；独立评测→实验控制标「评分」；实验控制→共同运行时标「基线／候选执行」；实验控制→持久化记录标「保存」；持久化记录→实验控制标「经验反馈」。将实验控制与运行时的回路放在左侧或卡片间留白，所有箭头互不交叉、不压字。下方共用能力带标题「KernelRuntime 与 S / F / C / P」，短注「登记任务资产 · 固定权限与验证」，它是运行时能力说明，不是额外流程节点；不接流程箭头。底部短注「评测参考不进入生成输入」。评测卡只有任务输出输入，不接任务输入或经验 Wiki。不画外部 Agent 接口，不写未来能力。
```

## architecture-en

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: architecture-en.png
supporting_reference: architecture-zh-CN.png
---
# 画图提示：architecture-en

Image 1 is the user's original style reference. Image 2 is the completed Chinese runtime-architecture diagram: translate image 2 into English, preserving its exact composition, all six main arrows, the two internal runtime arrows, card colors, icons, and the bottom capability strip. Replace ALL Chinese labels with the exact English below. Increase card width or wrap long subtitles to keep text legible without clipping. Create a readable English application-architecture overview titled "DarwinAgent Runtime Architecture". Five main cards and a shared-capability strip below, mirroring the requested Chinese architecture. Left card "Task input", identifier "DatasetAdapter", subtitle "Records, questions & sources". Center blue card "Shared runtime", identifier "Pipeline", internally "Extract facts" → "Source graph" → "Query & answer", subtitle "ExtractionAgent · AnswerAgent". Right card "Independent evaluation", identifier "Evaluator", subtitle "References & scoring" with a small lock icon. Lower-center card "Experiment control", identifier "ExperimentRunner", subtitle "Propose, admit & adopt". Lower-right lavender card "Durable records", subtitle "Asset versions, scores, decisions & Wiki". Exact directed connections: Task input→Shared runtime labeled "Generation input"; Shared runtime→Independent evaluation labeled "Task output"; Independent evaluation→Experiment control labeled "Scores"; Experiment control→Shared runtime labeled "Baseline / candidate run"; Experiment control→Durable records labeled "Save"; Durable records→Experiment control labeled "Experience feedback". Keep all six arrows unambiguous, route the runtime-control loop in whitespace, avoid crossing arrows and text. Shared capability strip at bottom: "KernelRuntime + S / F / C / P" with subtitle "Registered assets · Fixed permissions & validation". This strip describes the runtime capability, not a flow step; it has no flow arrows. Footer: "Evaluation references never enter generation input". All labels English, no Chinese. Do not invent external-agent plugins.
```

## asset-boundary-zh-CN

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: asset-boundary-zh-CN.png
---
# 画图提示：asset-boundary-zh-CN

参考图只作为视觉风格参考。沿用它的白底、自然黑色手绘线条、浅蓝浅绿浅黄浅紫浅橙色卡片、清晰手写感标签与简洁图标。整体横向宽屏，留白充足，适合 GitHub README 和中文技术文章手机阅读。文字准确完整，不添加版本号、分数、增长曲线、水印或无关装饰。创作中文边界信息图，标题「DarwinAgent：可以改什么？」。上半部浅蓝手绘大框标题「可修改的任务资产」，其中横向并排四张独立小卡，不连箭头：第一张「S · 模式」，副标「类型与关系」，结构图图标；第二张「F · 查询函数」，副标「只读、受限执行」，工具图标；第三张「C · 任务检查」，副标「图与回答的检查意见」，检查板图标；第四张「P · 角色提示词」，副标「固定登记槽位」，对话文档图标。上框整体通过居中向下箭头标「登记与验证」接到中央浅黄卡「候选准入与行为试跑」，再通过向下箭头接到下方浅紫大框。下方大框标题「固定的框架与独立评测」，其中横向并排四张独立小卡，不连箭头：第一张「执行与权限」，副标「Pipeline 与固定算子」；第二张「固定验证」，副标「类型、来源与状态」；第三张「独立评测」，副标「评测参考与评分契约」；第四张「采纳与发布」，副标「固定规则、原子版本」。下框角落用小锁图标强调固定边界。底部短注「内置演示只修改 P；框架不训练模型权重」。所有中文准确，只有 S、F、C、P、Pipeline 保留英文，不要拼错「槽位」「契约」。
```

## asset-boundary-en

```text
---
style: 手绘插画
ref: https://img-1302474103.cos.ap-nanjing.myqcloud.com/article-img-16d576643bb37af9.png
provider: codex
model: codex-image-gen
output: asset-boundary-en.png
supporting_reference: asset-boundary-zh-CN.png
---
# 画图提示：asset-boundary-en

Image 1 is the user's original style reference. Image 2 is the completed Chinese asset-boundary diagram: translate image 2, preserving its exact layout, icons, containers, two downward arrows, soft pastel fills and hand-drawn typography. Replace all Chinese text with the following English labels. Create an English asset-boundary infographic titled "DarwinAgent: What Can Change?". Upper pale-blue hand-drawn container titled "Mutable task assets" holds four independent side-by-side cards with no arrows between them: "S · Schema" / "Types & relations", "F · Functions" / "Read-only restricted queries", "C · Task checks" / "Graph & answer feedback", "P · Prompts" / "Registered role slots". Each has a small structure/tools/checklist/document icon. One central downward arrow from the whole upper container labeled "Register & validate" leads to a pale-yellow card "Candidate admission & behavioral trials". One downward arrow then leads to the lower pale-lavender container titled "Fixed framework & independent evaluation", with a small lock icon. Four independent side-by-side cards with no arrows between them: "Execution & permissions" / "Pipeline & fixed operators", "Fixed validation" / "Types, sources & status", "Independent evaluation" / "References & scoring contract", "Adoption & publication" / "Fixed rules & atomic versions". Footer: "The packaged demo changes P only; model weights are not trained". All English, no Chinese. Preserve the reference's approachable hand-drawn style and strong legibility.
```

## evolution-loop-zh-CN

中文循环直接复用用户提供的原图，不重新生成。原图包含采纳／拒绝分别进入经验知识库，以及经验回到下一轮提案的三条必要反馈连线。
