# Tool / Docs 重构执行计划

用户于 2026-10-06 确认范围：synthetic Markdown 产品文档；section/chunk 导入；Cases/Docs 分别搜索限额，共享 agent_steps；bootstrap 计入 Case 和 step；按工具及规范化 query 去重；旧 compute 不升级，历史 waiting-review 兼容。

子 agent 严格串行执行；主 agent 检查每阶段交付后才启动下一阶段。保留现有工单取消改动。禁止新增付费模型/embedding 调用、发布或破坏性数据操作。

| 顺序 | 任务 | 模型 / effort | 阶段门禁 |
| --- | --- | --- | --- |
| 1 | 五动作 Decision、Docs hit 契约、prompt | gpt-6-luna / high | schema/prompt 定向测试 |
| 2 | Markdown 文档、导入、独立检索索引与命名空间、示例数据 | gpt-6-sol / high | 导入幂等、正确 chunk、来源隔离、版本测试 |
| 3 | Tool rejection、预算、单 Graph、新版本 state/protocol、证据与审核、Recovery | gpt-6.1-sol / xhigh | 路由/预算/失败/旧审核及 PostgreSQL Recovery 回归 |
| 4 | 可选 LangSmith、自定义能力和拒绝观测、恢复关联 | gpt-6-sol / high | tracing 开关/故障注入；凭据可用时实连验证 |
| 5 | 整体验证、回归修复、启动及使用说明 | gpt-6.1-sol / high | 全量离线 + 可用真实 PostgreSQL/Milvus；明确未验证项 |

## 语义约束

- schema 拒绝未知动作/非法结构；业务 guard 拒绝合法结构的重复/额度/事实违规请求，并返回 Decision。
- step 限制约束全部决策和工具尝试；检索拒绝不占成功搜索额度。bootstrap 计一次 Case 搜索和一次 step。
- step 耗尽产生固定人工审核提案，绕过 Judge；active time 耗尽及 infrastructure/protocol failure 继续安全失败。
- 用户后续明确确认：首次 Judge FAIL 后若已无 step 执行 Repair，同样生成固定转人工提案直接进入审核，零 Repair 模型 / 第二 Judge 调用。
- Judge 首次 FAIL → 唯一 Repair → Judge；再次 FAIL → RunFailure。
- 保留 resume_gate、冻结契约、observed/estimated 分账、显式恢复和审核幂等。未知硬退出复合检索仍按既有安全策略拒绝续算。
- Docs chunk 可引用标识、版本/hash/synthetic 必须贯通 Decision/Judge/业务存储/审核展示。
- Trace 无凭据或关闭时正常执行，Trace 失败不能改变业务结论；外部 trace 实连与业务模型验收分别报告。

## 状态

- 阶段 1：已完成，schema/prompt 定向 21 passed；新 DECISION_PROTOCOL v3。
- 阶段 2：已完成，文档定向 4 passed（真实 PG + Milvus 替身）；配置/schema 3 passed。真实 Milvus 未启动，尚未实连验收。BM25 可用，dense 未就绪，hybrid 显式跳过 dense。
- 阶段 3：已完成。最终离线 268 passed / 176 skipped；真实 PostgreSQL 442 passed / 2 skipped / 0 failed。报告：`D:/AnalyzeAgent/phase3-toolgraph-final-db-fixed.xml`。模型与检索使用替身。
- 阶段 4：已完成，定向 + 真实 PostgreSQL 恢复回归 123 passed / 0 failed。报告：`D:/AnalyzeAgent/refactor-verification/trace/phase4-targeted.xml`。LangSmith 云端缺凭据未验收。
- 阶段 5：代码与本轮授权验收已完成。用户确认的 Repair 无 step 固定转人工路径支持 review/standalone、纯程序节点零模型超时估算、清除旧失败候选、业务保存崩溃恢复与审核幂等；active time 耗尽与二次 Judge FAIL 仍失败。修复 M4 prepare/score 离线隐式读 DB、隔离 factory 注入与缓存 Docs 引用验证；Docs 确定性配置/协议错误安全终止。
- 最终代码全量：离线 288 passed / 179 skipped；真实 PostgreSQL 465 passed / 2 skipped / 0 failed。最终报告为 `D:/AnalyzeAgent/refactor-verification/final-tools-docs/offline.xml` 与 `postgres.xml`；过渡 `postgres-before-docs-fatal.xml` 不作为最终证据。使用说明与完整范围见[本次交付报告](tool-docs-refactor-verification.md)。
- 外部验收仍未完成：按用户要求本轮不实连 Milvus；无 LangSmith key，云端未实连；不新增真实模型/embedding 付费调用。Docs dense 未实现，hybrid 仅显式降级到 BM25，不能宣称双通道已验收。
