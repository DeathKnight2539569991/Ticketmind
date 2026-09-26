# 2026-09-24 审查优先问题修复

本次按全项目审查的 A01—A05 修复，保留单 Agent、模块化单体和人工审核流程。真实模型配置沿用阿里百炼 `qwen3.8-flash` Decision / `deepseek-v4.1-flash` Judge。未部署、未迁移共享业务 schema、未重建共享检索集合。

## 修改内容

| 审查项 | 修复 | 验证目标 |
| --- | --- | --- |
| A01 Milvus 测试隔离 | Dense 和 BM25 同时使用本轮 UUID 集合；测试客户端拒绝非本轮集合的写入/删除与未知操作；共享冻结索引对照使用只读客户端 | 独立集合真实发布、检索、修复、停用和清理；离线证明越界写入在请求发出前被拒绝 |
| A02 错误日志暴露模型原文 | 常规运行失败日志仅记录运行编号、阶段、稳定错误码、异常类名，移除完整异常链 | 合成 ValidationError 和供应商异常均不把标记文本写入日志，运行证据与 usage 仍保存 |
| A03 关闭异常覆盖结果 | Milvus close 失败写入 `usage.cleanup_errors` 并记录无原文告警，保留成功提案或原 RunFailure | 成功/失败两条路径均保留证据、用量和原始原因 |
| A04 旧测试契约 | 同步显式 final_action、四字段知识文章、消息/文章限制、当前模型协议、数据模型、检索替身和界面控件 | 保留原状态、权限、幂等、并发、恢复及审核后才写入的断言 |
| A05 双模型评测入口 | M4 与 M2 acceptance 分别注入 Decision/Judge；Judge 有独立请求指纹、原始响应缓存、累计调用上限及失败记录 | 没有 Judge 额度时不发送 Judge 请求；失败不自动重试；精确缓存复用不新增调用；默认新调用额度为 0 |

当前评测默认模型和新参数见 `docs/m4-call-budget.md`、`docs/m2-acceptance.md`。历史结果保留原日期、模型和协议，不改写为当前模型成绩。`check_m2_followup.py` 仍是旧历史专用脚本，尚未迁移独立 Judge；它不能用来证明当前双模型真实追问链路，不能照旧预算命令直接付费复跑。

## 修复旧测试后发现的知识重试问题

原先 `/knowledge/{dataset}/{source}/retry` 对同一幂等键再次调用时，无条件再次修复索引并推进版本。现在已完成的重放直接读取现有结果，不重复推进版本或写索引。

同时保留中断恢复：对 active 知识请求重试时，在同一 PostgreSQL 事务中保存 `index_repair_pending`。若提交后、开始同步前进程中断，同 key 重放或默认 reconcile 都会继续修复；同步成功后清除该标记。仅凭 active 状态不能判断本次重试已完成。已有知识在待修复期间仍按现有状态及索引校验规则参与检索。

## 验证记录

最终完整测试集 **286 项全部通过，0 失败、0 跳过**，耗时 110.27 秒。包含真实随机 schema PostgreSQL/HTTP/AppTest，以及真实 Milvus 的知识发布、Dense/BM25/Hybrid 检索、索引修复、停用、双通道孤儿清理和共享冻结索引只读排名对照。Milvus 服务版本为 `3.0-20260902-658cbd1689`。两条第三方依赖弃用提示不影响本轮通过结果，未顺带升级依赖。

本轮证据目录为 `D:\AnalyzeAgent\review_20260924\fix_validation`。pytest 的模型和向量都是明确替身；即使开启 Milvus 开关，也不新增真实模型/Embedding 调用。所有数据库写入在 UUID schema，Milvus 发布测试只清理本轮创建的随机集合。

复现命令（从 app 目录运行，要求本机 PG/Milvus 和历史冻结索引可用）：

```powershell
$env:TICKETMIND_RUN_DB_TESTS = '1'
$env:TICKETMIND_RUN_MILVUS_TESTS = '1'
uv run --no-sync python -m pytest tests -q
Remove-Item Env:TICKETMIND_RUN_DB_TESTS
Remove-Item Env:TICKETMIND_RUN_MILVUS_TESTS
```

## 真实百炼集成回归

在用户再次确认配置和上限后，通过修复后的 M4 入口执行 004/005/006/018 四个合成案例：真实 HTTP 应用路径、只读 Milvus BM25、百炼 Decision/Judge、随机 PostgreSQL schema 与真实检查点。四例均成功进入 `waiting_review`，动作依次为建议解决、追问、转人工、追问，与既有标签一致；Judge 均通过其限定的四类检查。每例均验证 HTTP/数据库一致、同请求重放不增加运行，工单仍保持原版本和一条客户消息，没有发布回复或关闭工单。

本轮新增 **Decision 4 / Judge 4 / Embedding 0**。与审查阶段累计为 **Decision 12/20（其中最初 4 次连接失败）/ Judge 8/8 / Embedding 0**，Judge 授权上限已用完；没有追加请求、自动重试或删台账重置额度。本轮成功响应记录用量：Decision 输入 15,168、输出 1,189 tokens；Judge 输入 4,909、输出 24 tokens，合计 21,290 tokens。用量不是账单金额；此前连接失败的 usage 为空，无法据此断言费用为 0。

结果与用量保存在 `D:\AnalyzeAgent\review_20260924\fix_validation\paid-integration\results.json` 和 `usage-summary.json`，冻结配置在同目录 `manifest.json`。执行后只读核对确认四个回归 schema 均已清理，Milvus 无 `tm_test_` 测试集合残留。

四例动作匹配、Judge 放行、追问完整性是三个不同维度。005 仍未明确收集原始表头和其他预览错误/选项；018 询问了时间戳、日志和其他接口表现，但仍未完整收集既有诊断结论等信息。当前 Judge 本来不负责追问完整性。本次优先代码修复没有调整 Decision 的语义策略，不声称解决所有模型内容质量问题，也不能将四例动作一致表述为普遍准确率 100%。

## 后续路径回归 · 2026-09-26

另经用户确认，从 9 月 19 日的 40 题模型对比集选取 005/038/011/037（`TM-COMP-2026-09-` 前缀），完成 5 次新的真实 Agent 运行。新增 Decision 5、Judge 5、查询 Embedding 1、文档 Embedding 0，合计供应商报告 26,505 tokens；这是独立授权批次，不改写上面的历史台账。

审核发布→关闭→知识沉淀和真实 Embedding/Hybrid 两条路径直接跑通；追问续接因本轮测试夹具的审核枚举拼写错误中断，保留失败记录并恢复同批真实首轮结果后完成第二轮，不能称为一次无中断双轮实测。再次检索、详情读取和 Judge 修正均未自然触发，仍未获得最新代码下相应分支的真实模型覆盖。未据此声称完整 Agent loop 验收通过。

详细报告和原始证据位于 `D:\AnalyzeAgent\eval_runs\loop_paths_20260926\REPORT.md`。本轮使用当前生产源码与提示词，未修改它们；所有测试 schema 与 UUID Milvus 集合已只读核对清理完成。
