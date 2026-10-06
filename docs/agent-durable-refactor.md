# Agent durable execution 重构记录

## 已确认边界

- 分阶段串行实现；当前阶段测试未通过，不进入下一阶段。
- 计算恢复由 reviewer 显式触发；启动不自动调用模型续算。
- 兼容旧 checkpoint，只保证旧架构已有的待审核结果恢复、审核 interrupt 恢复与已完成审核重放。旧 compute 中断不升级为可恢复计算。
- 不改 Prompt、模型配置、检索排序或业务数据库 schema；不 commit、push、部署；本轮新增付费调用为零。

## Phase 0：基线（2026-10-05）

实际仓库为 `D:/AnalyzeAgent/app`，Git 基线为 `e4a656eadd711b73b58dc6abedcbdfbdd3a0464c`。开始时仅有用户未跟踪文件 `docs/project-iteration-retrospective.md`，保留不动。

实际执行路径：业务层创建 running 并提交事务 → ReviewWorkflow 的 compute → AgentRunner → 无 checkpointer 的 retrieve/decide 图 → bounded_decision 的工具 while 和 judge/最多一次 repair → review interrupt → 业务层保存 waiting_review。审核先保存不可变记录，图恢复在事务外，业务事务原子应用消息、版本、工单状态与 applied_at。

外层 `compute → review` 使用 PostgresSaver 和 `durability="sync"`。Agent 内部没有 durable boundary。计算中断无完整可恢复提案时进入 failed；只有合法 review interrupt 的已有 output 可以恢复待审核。旧 review 及已完成图的审核重放不得调用模型。

本机实际依赖：langgraph 1.2.11，langgraph-checkpoint-postgres 3.1.2。已检查安装源码 `pregel/_loop.py` 的 continuation 分支和实际签名；使用确定性节点验证同 thread `invoke(None, config, durability="sync")` 恢复失败计算，`Command(resume=...)` 恢复审核。

测试证据位于 `D:/AnalyzeAgent/refactor-verification`：

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 默认全量 pytest | 178 passed，108 skipped，0 failed | 模型/检索替身；DB 未启用 |
| DB 启用全量 pytest | 284 passed，2 skipped，0 failed | 真实 PostgreSQL/事务/checkpointer；模型/向量替身；Milvus 测试未启用 |
| 恢复 API 探针 | InMemorySaver、PostgresSaver 均通过 | retrieve=1；失败 decision 恢复后=2；审核恢复调用不增加 |

现有两个依赖弃用 warning 属于基线。默认 `scripts/verify_project.py` 因缓存输出目录权限失败，改用同一环境直接调用 pytest，不改原脚本。XML：`baseline-unit.xml`、`baseline-db.xml`；探针：`probe_resume.py`。

官方参考：https://docs.langchain.com/oss/python/langgraph/checkpointers 。真实版本行为以安装源码和上述探针为准。

## 后续阶段及风险

1. Durable State / Runtime：纯数据 state、显式 counters/queries/details/usage/repair 状态、替换式节点更新；保持现有行为。风险：model adapters 输入形状与缓存指纹、共享 mutable 数据、预算生命周期。
2. Initial retrieve 提升：`retrieve → decision_compute → review`，暂保留 while/judge 编排。真实 PG 调用计数验证已完成检索不重跑；业务续算入口仍在 Phase 5 才启用。
3. 工具 loop：decision、search_cases、get_case_detail 及受控路由。保持初始检索/决策/工具步数语义和所有 guard。
4. Judge / Repair：显式节点，最多一次 final-only repair，第二次 reject/fatal failure 安全失败。
5. Recovery：显式认领短事务 → 事务外继续图 → 重新检查版本并落库；区分合法计算 checkpoint、review、完成、缺失/非法、fatal 和旧 checkpoint。启动只分类，不续算。
6. 清理：移除旧 inner graph/while 编排；保留纯校验函数、旧审核兼容；全量回归和更新文档。

Active compute budget 跨节点累计，不按节点重置；人工等待和服务离线不计入。未完成节点可能重复执行，其调用费用/未 checkpoint 的耗时与审计存在恢复限制，后续阶段明确记录，不能声称模型 exactly-once。

## Phase 1：Durable State / Runtime

修改 `agent/state.py`、`agent/runtime.py`、`agent/tools.py`，新增 `tests/test_agent_state.py`。图拓扑和业务恢复行为不变。

- 版本化纯数据快照与临时 typed 投影，保留知识命中的 revision/hash/metadata，拒绝资源、非法计数与非有限数值。
- `AgentRunner.__call__` 委托单次 `AgentExecution`，能力、资源生命周期、usage、stage、总预算移为明确方法。
- while 暂保留；显式保存去重列表、steps/rounds、Repair 次数、Decision、candidate Proposal、Judge/反馈。独立输入与替换式快照防止共享 mutation。
- 最终 proposal 与 candidate 分开，护栏失败仍无可审核 proposal；新 Judge 前清理旧结果。
- `ExecutionBudget` 支持累计 active 时间，停止后的等待不计时；跨进程恢复尚未启用。

验证：定向 **149 passed**；真实 PostgreSQL 全量 **310 passed、2 skipped、0 failed**。XML：`phase1-agent-targeted.xml`、`phase1-db.xml`。模型 payload 往返前后相同；无新增付费调用。`git diff --check` 通过。

## Phase 2：initial retrieve durable node

生产执行路径为 `retrieve → decision_compute → review`，同一个 PostgresSaver、sync。Runtime 从纯数据初始化独立 node scope；资源仅在检索时取得，关闭并记录诊断后输出 checkpoint。while/Judge/Repair 暂保留在 decision_compute。旧 callable-only 演示入口和旧审核格式保留。

修改 `agent/review.py`、`agent/runtime.py`；新增 `tests/test_agent_durable.py`、`tests/integration/test_agent_durable_postgres.py`。必要适配调整为 `agent/dev_acceptance.py` 的显式 initial/research factory phase，以及 `scripts/check_m2_followup.py` 的整个运行只遮挡首次检索。

首轮全量门禁 **314 passed、2 failed、2 skipped**：初始化失败 stage 回归、验收适配器重复重置首次调用。两者基线通过，确认为本阶段回归；修复后定向 **133 passed**，全量真实 PG **316 passed、2 skipped、0 failed**（`phase2-db-fixed.xml`），才进入 Phase 3。未削弱测试断言或预算。

真实 PG：关闭 saver/pool 后重建 workflow/runtime/saver，同 thread 恢复，retrieve=1、decision=2、judge=1；恢复审核与重放模型调用不增加。JSON-only、成功节点审计/usage/steps 不重复；80 秒成功检索保留，离线 10000 秒不计时。缺失、旧 compute、非法 checkpoint 在能力调用前拒绝。

限制：`continue_compute` 仅图级接口，业务入口仍未开放。当前 graph-only continuation 采用成功 checkpoint 耗时；观察到的失败累计耗时在 `RunFailure.usage.execution_budget.observed_elapsed_seconds` 保留，Phase 5 必须合并。fatal/配置漂移分类与 hard crash 的未知耗时策略也留到 Phase 5，不声称现阶段恢复完整。

## Phase 3：Decision / tool durable loop

修改 `agent/review.py`、`agent/runtime.py`、`agent/tools.py` 及两个 durable 测试文件。生产路径变为 `retrieve → decision → search_cases/get_case_detail → decision → judge_compute → review`。Decision 不执行工具；共用原工具 guard，拒绝写入审计并经 deterministic escalation、Judge、Review。Judge/Repair 暂留在 judge_compute，旧 standalone 编排留到 Phase 6 清理。

首轮定向 118 passed、3 failed：一个 audit sink/回调快照共享回归及两个新增测试 fixture 错误；均修复。最终定向 **124 passed**，全量真实 PostgreSQL **331 passed、2 skipped、0 failed**（`phase3-db.xml`），`git diff --check` 通过。

真实 PG B/C/D：成功 search/detail 后下一 Decision 失败，关闭 saver/pool 并重建 workflow/runtime 后恢复。工具各执行一次、Decision 三次（成功、失败、重试）、Judge 一次；已持久化 steps=3，恢复 steps=4；成功审计两条不重复，成功 Decision usage 从一条变为两条。历史 checkpoint 证明 Decision 完成边界尚未执行工具。审核恢复和重放模型调用增量为零。

生产图定向覆盖步数、search/detail 上限、重复 query/detail、虚构 query、未知来源、澄清上限、未知 proposal evidence、工具失败与 final-only repair。Phase 5 的业务恢复和预算合并尚未启用。无新增付费调用。

## Phase 4：Judge / Repair durable nodes

修改 `agent/tools.py`、`agent/runtime.py`、`agent/review.py` 与两个 durable 测试文件。生产图改为 `retrieve → decision ↔ tools → judge → repair → judge → review`，Judge pass 后才设置最终 proposal/output。共用原校验规则，Repair 最多一次、final-only、消耗原步骤和总 active 时间，不执行工具；第二次 reject 或 Judge/Repair 协议失败安全失败。生成新候选前清理旧 judge_result。

新增 16 例。首次定向 172 passed、1 failed，归因新增测试传入配置禁止的 max_agent_steps=2；改用合法 max4，通过 detail/decision 耗尽步骤后仍断言同一 step-limit 安全失败。最终定向 **175 passed**；全量真实 PG **347 passed、2 skipped、0 failed**（`phase4-db.xml`），`git diff --check` 通过。

真实 PG 新建 saver/pool/workflow/runtime 验证三种未 checkpoint 边界：首次 Judge（retrieve1、Decision1、Judge2）；Repair（retrieve1、Decision/Repair3、Judge2）；Repair 已 checkpoint 后第二 Judge（retrieve1、Decision/Repair2、Judge3）。已完成的节点不重跑，未完成节点允许重跑；审核恢复和重放模型调用增量均为零。预算/steps/usage/audit 及旧 semantic tests 保持。

Phase 5 的业务认领、已观察失败耗时合并与 fatal/配置漂移分类尚未实现。硬退出时未观察耗时策略已集中询问用户，尚待答复；不预设该业务选择。旧 standalone 编排留到 Phase 6 清理。本阶段新增付费调用为零。

## Phase 5：显式 Recovery（已完成，2026-10-06）

本阶段修改 `agent/review.py`、`agent/runtime.py`、`tickets/processing.py`、`tickets/recovery.py`、`tickets/reviews.py`，以及 API runner 注入、恢复路由、工作台恢复文案和 ProcessingRecovery 注释。README/architecture 与当前行为同步；未改变业务 schema。新增 `tests/integration/test_agent_recovery_postgres.py`，扩展 durable 测试。

- reviewer 显式恢复：认领短事务 → 关闭业务 Session/释放连接 → 同 thread 续算 → 短事务重检 ticket version/status、run status、active run → 保存 waiting_review。恢复不发布；审核仍使用原不可变记录和原子应用。
- 启动只读取并分类 checkpoint。合法计算保留 running 并标记 explicit_recovery_required，不调用能力；合法 review output 恢复 waiting_review；未完成审核要求重试原审核；旧 compute、缺失/非法、fatal 或版本失效均不重新跑整个 Agent。
- 持久化纯 JSON fatal descriptor 至 END 后，向当前调用重新抛出 runtime-only 的原 RunFailure；协议、Guardrail、认证及检索配置/协议错误沿异常链分类。业务失败状态未保存时，重启仍不能重算 fatal。
- 冻结并核对 snapshot、agent/corpus/model/protocol、limits、retrieval 参数、dataset 和端点身份 SHA256；端点和凭据不明文进入新 contract。配置漂移在能力调用前拒绝。计数上限、queries/details/来源一致性与节点位置一起检查。
- 已观察失败 active 耗时在框架 update_state 中保存一次纯数据诊断（实测确认 next 不变），Command(update=...) 合并后继续未完成节点，未对每个节点手工创建 checkpoint。80 秒检索 + 2 秒失败 Decision 后剩余 8 秒；多次初检失败累计 2→4→6；离线时间不计入。诊断保存失败时可匹配业务 node/checkpoint_id 观测；策略收尾已用真实 PG 注入验证该 fallback。
- 成功 steps/audit/usage 使用替换式 checkpoint 数据；失败 attempt 的实际回报用量按差量独立保留，缺供应商回报明确 unknown。callable-only 旧 compute 不追加新诊断，保持原 usage 精确契约。
- 自定义适配器非法 usage（运行时对象/NaN）被拒绝；安全诊断替代无法序列化的 usage，fatal 终止标记仍持久化，不把非法对象放入 state。该路径新增两项内存 checkpoint 测试，不称真实供应商验收。

验证过程：定向 **183 passed**；首轮全量 **369 passed、2 failed、2 skipped**，两处原日志用量精确断言回归，修复新诊断误作用于旧 compute 后，必要定向 **66 passed**，全量 **374 passed、2 skipped**。root 对非法 usage 新增失败复现和修复，最后定向 **102 passed**；最终当前代码全量真实 PostgreSQL **376 passed、2 skipped、0 failed**（`phase5-final-db-pending-policy.xml`，82.88 秒）。`git diff --check` 通过。仅基线依赖 warnings。

策略收尾前新增 27 项真实 PG 恢复用例，覆盖 A–H 核心计数、审核/恢复幂等、认领后崩溃同 key 重试、并发 live 阻挡、版本变化、冻结配置漂移、fatal 保存前中断、缺失/损坏 checkpoint、累计预算及原始旧 compute→review 图 fixture。实际业务连接释放通过能力 callback 的 NOWAIT/连接占用断言验证。旧 review 和已完成审核重放模型调用增量为零，消息只应用一次。

用户选择的策略 1 已实现：硬退出后的未知调用耗时，按冻结配置的 effective timeout 保守扣减；上限不可靠则拒绝续算。扣减明确标记 conservative/estimated，不能混入 observed；扣减耗尽预算后直接失败，零能力调用。单模型节点 Decision/Judge/Repair 的冻结上限为 min(30 秒, 原剩余预算)；检索/详情复合节点的未知耗时不具备可靠总上限，拒绝恢复。

恢复扣减先持久化 prepared，再由唯一 resume_gate 持久化 started 并路由原未完成节点。prepared 崩溃复用扣减；started 后再次硬退出按新的 checkpoint/attempt 再扣减。started 只表示允许进入调用阶段，不证明供应商已收到请求，记录仍是估算。恢复更新通过 update_state 的显式路由归属安排 gate，不执行初检。总 active 时间 = observed + estimated；离线和人工等待不计入。

首轮策略定向测试 138 passed、1 failed，定位为 started 边界再次恢复时框架推导 writer 导致绕过 gate；修复唯一 gate 路由后通过。补测首轮两项 compound fixture 的 writer 错误已修复并断言实际 next，未弱化业务断言。增加真实 PG 的估算后已知失败分账、诊断写失败业务 fallback、另一 active run 阻挡、三类未知复合调用拒绝、未知 Judge/Repair 原节点恢复、估算耗尽零调用、prepared/started 再崩溃，以及预算终止标记写入后业务保存崩溃的显式/启动恢复。后者保留 execution_budget_exhausted 和扣减账本。非法 usage 替代诊断也验证估算不会误标为 observed。

最终定向 **110 passed、0 failed、0 skipped**（`phase5-policy-final-targeted.xml`，19.37 秒）；全量启用真实 PostgreSQL **394 passed、2 skipped、0 failed**（`phase5-policy-final-db.xml`，88.29 秒）。模型/向量能力使用确定性替身，两个真实 Milvus 用例未启用；仅两项基线依赖 warning。`git diff --check` 通过。测试证据位于 `D:/AnalyzeAgent/refactor-verification`。

**Phase 5 已结束，按用户要求停止，Phase 6 未开始。** 旧 standalone inner graph/while 和动态 graph compilation 保留，待后续清理；本次不声称整个六阶段重构已完成。当前未完成节点仍可能 at-least-once 重跑，估算不等于供应商实际用量，不保证模型调用 exactly-once。旧 checkpoint 仅保留原有审核恢复能力，旧 compute 不升级。本轮没有付费调用、commit、push 或部署；Git HEAD 仍为基线 e4a656eadd711b73b58dc6abedcbdfbdd3a0464c。


## Phase 6：单 workflow 清理（2026-10-06）

重构前（Phase 0）：

```text
ReviewWorkflow: compute → review(interrupt) → END
                  └── AgentRunner
                       └── 临时无 saver 图：retrieve → decide
                                                      └── bounded_decision while
                                                           └── judge / repair while
```

重构后（Phase 6）：

```text
START → retrieve → decision ──search_cases/get_case_detail──→ decision
                      │
                      └──final──→ judge ──reject──→ repair → judge
                                     │ pass
                                     ▼
                              review(interrupt) → END
```

所有生产节点共用原 PostgresSaver，sync；fatal 分支到 END，未知硬退出恢复由 prepared → resume_gate(started) → 原 Decision/Judge/Repair 调用位置控制。不存在第二套业务 checkpoint 表或新的业务 schema。

- 删除 `agent/graph.py` 的 `build_ticket_graph`、`AgentExecution.run/decision_compute`、`tools.bounded_decision/finish_proposal`；保留 query/proposal/judgment 校验、escalation、只读工具执行、retrieval/model adapters。所有路由只由 `ReviewWorkflow` 的显式图决定。
- Workflow 初始化时编译一次；`graph()` 返回同一实例。runner 和 fatal 原异常 sink 在 `WorkflowContext` 按 invocation 注入，资源/函数/Session/计时器不进入 state、checkpoint 或共享 closure。并发 invocation 使用各自 runner、异常 sink 和 thread_id。
- `AgentRunner.__call__` 保留直接计算 API，缓存同一拓扑的无 saver Workflow，Judge 通过后 END；此入口不是持久化恢复/发布入口。初始化锁避免并发首次调用重复编译。业务 start 仍要求 review interrupt；业务数据库发布与关闭仍要求人工确认。
- 旧 callable 演示/测试入口保留为 retrieve 边界的 output adapter；纯数据 `adapter_output_only` 阻止失败入口被误认为新 durable compute。旧审核 checkpoint 通过同名 review 与原 output 恢复，原 compute→review 真图 fixture 保留；旧 compute 和 callable 中断都不升级为可恢复计算。
- 旧 decision 能力检查迁至 `agent/dev_workflow.run_decision_workflow`：用显式 capability subclass 注入预先检索的证据并复用生产图，无独立 while/图编排。旧 retrieval-only 检查直接调用 `retrieve_ticket`，仍测试向量缓存、单次调用与 Milvus 失败分支；没有偷偷引入真实模型调用。

Durable boundary 从外层整个 compute 提升到 retrieve、单次 Decision、单个 search/detail、单次 Judge、唯一 Repair 与 review。Phase 6 只删除重复路径并稳定图生命周期；Phase 2–5 的恢复边界、冻结配置、预算、计数和业务幂等语义保持。

Crash/restart：已成功 checkpoint 的节点不再重跑；失败/硬退出的当前未完成节点仍可能 at-least-once。模型或检索已返回但尚未保存 checkpoint 时仍可能重发，供应商费用和无法观测的用量不能声称 exactly-once。成功逻辑 steps/audit/usage 使用替换式状态；失败 attempt 独立记录。单模型未知耗时按冻结 effective timeout 保守估算，observed 与 estimated 分账；复合检索/detail 未知耗时没有可靠上限时拒绝续算。离线/人工等待不计 active 时间。启动仅分类，reviewer 显式续算；审核重放无模型调用，消息原子应用一次。

新增生命周期验证覆盖：共享编译图上的两个并发 runtime（成功与 fatal）的 runner/failure 隔离、全部 checkpoint values/metadata 的纯 JSON、重复直接 Runner 调用不泄漏状态且不重复编译、禁止把 standalone compute 用于 durable Workflow、callable 失败分类和恢复零能力调用。原真实 PG A–H、旧审核恢复/消息幂等、预算 gate 与 fatal fixture 继续保留。

验证：首轮定向发现无 saver 时 sync 引发安装版本 LangGraph 生命周期错误、update_state 路由缺少 Runtime 注入，以及迁移适配器 evidence 不完整；已修复，无 saver 入口不传 durability，生产 durable 入口仍 sync，纯路由使用 config 而资源使用 context。首轮全量 **391 passed、3 failed、2 skipped**：一处 crash fixture 不接受新 context 关键字、两处 callable 失败 usage 精确契约回归；分别迁移 fixture 签名和限制新失败诊断仅作用于 durable AgentRunner。生命周期新增测试首轮两项无客户消息 fixture 错误已修正合法输入。

修复定向 **154 passed、0 failed**（`phase6-final-targeted.xml`，14.26 秒）。最终全量真实 PostgreSQL **398 passed、2 skipped、0 failed**（`phase6-final-db.xml`，95.06 秒）。全量启动后补强的 callable continue_compute 零能力断言与 checkpoint metadata JSON 检查另行通过，最终生命周期 **4 passed、0 failed**（`phase6-lifecycle-final.xml`，2.56 秒）。模型和向量能力均为确定性替身，两个真实 Milvus 用例未启用，live 模型验收不在本阶段范围；仅两个基线依赖 warning。`git diff --check` 通过，生产 Agent 目录静态搜索仅一处 StateGraph/compile，无旧 graph/while API 调用点。

**Phase 6 已完成，六阶段重构的当前授权范围已全部实现并通过门禁。**没有付费调用、commit、push 或部署；用户 `docs/project-iteration-retrospective.md` 保留不动。
