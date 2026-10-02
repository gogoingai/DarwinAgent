# Oak 0.2 移植与兼容说明

本轮已经验证：Python 3.11、macOS 下独立安装 wheel，仓库外运行第三领域，搬迁内核后执行受控函数。尚未验证跨机器和 Windows；生成函数的可靠超时当前要求 `fork`。进程超时与 AST 检查不等于任意不可信 Python 的安全隔离。

## 接入方式

核心发行包包含 `oak` 与 `oak_domains`，不包含 `datasets`。通用依赖与基准依赖分开；形式推理按需安装 `formal` extra。源码中的基准入口仍从本仓库运行，不能把它们当作已经独立发行的领域应用。

- 输入使用 `CaseInput / CorpusBlock / QuestionInput`，参考答案由适配器保留在评测侧。
- 用 `Schema` 声明类型、主键、属性及关系；`build_graph` 默认拒绝非法候选。显式选择 `on_invalid="isolate"` 时，必须检查 `graph.graph["validation_errors"]`。
- 用 `KernelAssets` 登记 S/F/C/P/H、检查实现和依赖，导出 `KernelBundle`；搬迁后重新装载并校验摘要。
- `BuildEngine` 与 `InferenceEngine` 提供固定身份的阶段编排，具体领域能力通过回调注入。这两个入口尚不是完整的自动 B1—B4 学习控制器。
- 对受控的单函数 Python 资产，使用 `KernelRuntime` 执行已登记的 F。其他含包内相对导入的领域模块，目前只支持资产保存和摘要验证，尚不能保证脱离应用包执行。
- `Patch` 只允许 S/F/C/P/H 的现有资产修改，候选在独立目录验证后发布；当前不支持自动增加/删除资产及自动评分采纳。

可运行示例为 [`examples/third_domain.py`](../examples/third_domain.py)：设备具有联合主键，图中有两位技师，内核搬迁后只返回与目标设备存在维修关系的技师。这个示例不读取基准或 gold。

## 与旧代码的区别

1. 默认产物目录是调用者当前目录下的 `runs`，应用应显式设置 `Config.work_dir`。
2. 领域模型角色由 `Config.role_tiers` 注入；LoCoMo 的角色登记位于其适配配置，核心不登记基准角色。未指定 fast 服务时继承主服务。
3. 思考开关、思考余量和空输出处理由配置指定。更换提供商时显式设置这些策略，不将评测角色的请求策略混入生成调参。
4. 节点身份使用有类型的结构序列化。旧图不能通过更改文件名升级；新运行需按新的依赖身份重新构图。
5. 旅行图扩展从 `oak_domains.travel_planning.graph` 导入。旧的 `oak.kg.graph` 名称保留带弃用警告的兼容入口。
6. `answer-evidence-integrity` 只保证引用完整性，不宣称已经证明答案的全部语义；语义支持需要领域审查。执行失败与语义拒答分开表示。
7. 预算登记在 `Config.namespace_limits`，同时约束匹配的所有 scope；持久计数按逻辑调用预留，跨客户端及进程锁定。实际请求重试与 token 用量在台账中保留，不能把逻辑调用额度理解成费用上限。
8. HermiT 未完成属于 `UNVERIFIED`；要求形式校验的运行不会把它当作通过。

## 已验证范围

仓库内 26 个新增测试、16 个原 LoCoMo 测试和 28 个原旅行规划测试通过。独立安装环境下，15 个通用契约测试、3 个函数/关系执行测试以及第三领域示例通过。

LoCoMo 的真实效果单独记录在 `FRAMEWORK-REPAIR-LOG.md` 及 campaign 结果中。安装和契约通过不代表达到评测外错题率 <3%。
