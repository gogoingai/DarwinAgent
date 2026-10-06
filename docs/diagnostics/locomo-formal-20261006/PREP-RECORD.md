# 正式运行准备记录（PREP-RECORD）

准备时间：2026-10-06 深夜。执行方：Claude（操作者指令见 FORMAL-INSTRUCTIONS.md 与
会话指示：fast 层切 DeepSeek、补齐 conv-44、优先保障 HF 上传、完成终极检验）。

## 1. 代码固定

- `f1452af`：graph_rules.py conv-50 sources 单串拆分（阶段六外测实测）＋本目录
  指令文档（FORMAL-INSTRUCTIONS/protocol.json/split.json）入库。
- `f68342a`：conv-44 冻结记忆四件套（facts/graph/manifest/vector index）＋快照索引。
- 锁定 HEAD＝`f68342a3ac07bda3c674bf8ac97b263e342b3d97`。
- 回归：`python -m unittest discover -s tests -q` → **364 tests OK (skipped=7)**。
- graph_rules.py **全文件** sha256＝`6cfb495b7361afb4a978b7182f441ceb5f5279eaa659b1fe062824dc91cfafeb`
  （实验身份另有 builder 函数体摘要；此处按指令补全文件摘要）。

## 2. fast 层切换 DeepSeek（.env-only，无代码改动）

- 路由（三档，用户 2026-10-04 拍板架构）：strong＝glm-5.3（智谱）、
  middle＝MiniMax-M3.1-Flash-Preview（LOCOMO_FAST_*）、
  **fast＝deepseek/deepseek-v4-flash-fast @ https://api.commandcode.ai/provider/v1**
  （COMMANDCODE_*；.env 显式设 COMMANDCODE_MODEL，settings 默认 v4.1-flash 有
  思考失控史——OPTIMIZATION_LOG #40）。
- key：操作者 2026-10-06 提供两把；key#1 入 COMMANDCODE_API_KEY，key#2 以注释行
  存 .env 备援。**credential_identity 摘要含 fast key——正式运行中途不换 key**。
- 真实探测（precheck_agentic 的 fast_tier_json 探的是 tools＝middle 档，命名遗留，
  不触 commandcode 端点，故手工补测）：role='kg' 1 条 JSON 请求 →
  **6.0s 返回 {"ok": true}**，模型/网关连通正常。
- 负载预期如实说明：当前 LoCoMo wiki 路径无角色路由到 fast 档（kg/react/slots/
  plan_repair 无调用点）——DeepSeek 实际调用量≈0，限流风险极低；本路径负载集中在
  glm（answer/review/judge/bootstrap/proposal/wiki）与 MiniMax（extraction/tools）。

## 3. conv-44 冻结记忆补齐（158 题、675 语料块）

- **选型**：复用 `datasets/locomo/runs/conv-44/graph_60b2aa6e`（1611 事实）。
  依据：conv-44 运行时间线 gen1(graph_37000d7e, 1131 事实, 10-01 08:48) →
  full(graph_60b2aa6e, 1611 事实, 10-01 20:40) → closeout-r1..7（10-02，未再建图）
  ——60b2aa6e 是最终采纳版；陈述更完整（含职业等细节）、evidence_coverage 0.96、
  67 条无出处（4.2%，与 conv-26 快照 25 条同类、manifest 如实披露）；
  0 条字符串型 sources。
- **向量**：g_v_test 既定管线 `LOCOMO_GRAPH_PIN=…graph_60b2aa6e python -m
  pipeline.build_vector_index --conv conv-44`——钉死图零抽取、frozen schema 零 LLM
  调用，1611 条嵌入经 dashscope `qwen3.7-text-embedding-flash`（dim 1024），
  耗时 698s。与既有 9 快照同端点同模型；meta.dim=1024 为新版回填（旧快照记 0，
  系记录差异非配置差异）。
- **导入**：临时目录试导入全过后正式导入
  `datasets/locomo/snapshots/gvtest_v1/conv-44`；校验含 dia→语料块 675 全映射、
  facts↔图编号一致、向量⊆编号且全覆盖。manifest：
  snapshot_digest `61238ed77e011451e7bf54e4b8fbe2b4aebfae67a61258e264ac4bb85ebf2e5f`。
  快照 index.json 恢复为全 10 会话（importer 单会话覆写行为已人工修正）。
- **离线冒烟**：以 loop10 发布 bundle 的 S 做 rebuild_snapshot_graph →
  1716 节点/4777 边、1611 事实行全映射；semantic_search("安德鲁 新工作 金融分析师")
  命中前三均为安德鲁金融分析师事实；relative_date("上周")→2023-04-03 正常。

## 4. 冻结摘要（全部 10 对话；9 个既有与 protocol.json 原记录逐字节一致，0 偏差）

manifest.json sha256 前缀（完整值在 protocol.json）：
conv-26 1ae8032f / conv-30 02f7d695 / conv-41 81dfec7c / conv-42 7227423d /
conv-43 5037060a / **conv-44 5c0cdba8（新增）** / conv-47 f1eddc1a /
conv-48 a89d6d08 / conv-49 7cc20dd8 / conv-50 5a1f82c8。

## 5. HF 归档（操作者优先项）

首轮 xet 后端上传死于 `invalid shard … xorb not found`（且速率仅 ~30KB/s）；
改 `HF_HUB_DISABLE_XET=1` 经典通道重传：ARCHIVE-INDEX.json（+embed-cache 条目，
115 项）与 locomo-gvtest-embed-caches-20261006.tar.gz（436MB，sha256 c9aaf2c1…）
上传至 justis-xu/oak-agentic-runs——经典通道 ~2-4MB/s。完成状态见终报
（监视器在岗，成功事件已收到）。

## 6. 其余启动前核对（对照 FORMAL-INSTRUCTIONS「启动前处理」）

1. 当前三对话外测已结束并归档（ext_v0_final/ext_g1_final，终报已入库）——不重跑。 ✅
2. graph_rules/external_test 修改已固定（本记录 §1）。 ✅
3. fast 层切换＋重预检：切换见 §2；precheck 在运行根建立后执行（见下）。 ✅
4. conv-44 补齐＋10 快照摘要登记（§3/§4）；正式阶段结束后再核对一遍。 ✅
5. 不另开十轮小循环；B0/候选走框架真实图准入＋冒烟。 ✅（按指令执行）
