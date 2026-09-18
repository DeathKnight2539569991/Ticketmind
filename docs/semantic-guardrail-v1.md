# Semantic Judge v1：新的 guardrail 实验

原组合（Decision `glm-5.2` / Judge `qwen3.7-flash`）真实诊断已完成：**承诺样本能够拦截，但 Judge 仍误伤正常建议/历史询问，追问完整性仍不足，模型质量验收未通过。** 共 22 次真实调用，详细证据见 [2026-09-18 独立诊断报告](semantic-guardrail-runs/2026-09-18-live-v1/report.md)。

当前已切换为 Decision `glm-5.3` / Judge `deepseek-v4.1-flash`；当前 runtime 已移除独立 understanding 模型调用。新组合已完成一轮 [20 次真实请求诊断](semantic-guardrail-runs/2026-09-18-glm53-deepseek41-v1/report.md)：最小正反例 5/5 符合预期、036 修正后放行，但复杂文本误判仍在，005 Judge 协议失败，新的追问完整性样本因超时/截断未能验收。独立台账保留所有失败，不改旧报告。

GLM-5.3 不允许关闭思考；Decision 适配器已改为该模型启用思考、输出上限 4096，旧模型参数保持原样。缓存指纹同步记录实际参数。生产 30 秒阶段等待上限未改；本轮后续诊断使用了更长等待，仍出现截断，详见报告。下文配置为当前值，验收数字保留历史记录。

本次只实现四类语义检查，不处理追问完整性，不修改 M4 冻结结果、label、adjudication，也未调用付费模型。离线预设判定用来验证协议与状态流转，不能证明真实 Judge 的识别准确率。

## 运行与边界

正式入口仍为提交工单 → 发起处理 → 待人工审核 → 审核应用。配置默认值为：

```dotenv
TICKETMIND_MODEL=qwen3.7-flash
TICKETMIND_DECISION_MODEL=glm-5.3
TICKETMIND_JUDGE_MODEL=deepseek-v4.1-flash
```

MODEL 用于理解。Decision 与 Judge 复用现有业务空间和凭据，但分别指定模型；配置拒绝相同模型（包括大小写和标点别名）。不新增依赖或数据库迁移。正式处理会新增 Judge 请求；本次测试未发起这些真实调用。零付费 demo 仍是显式业务替身，不代表真实护栏效果。

`Decision → Pydantic/风险/来源/引用原文校验 → Judge → PASS → waiting_review`。

首次语义 FAIL 将原提案、violation 的类型/原文/原因反馈给 Decision，仅允许重生成最终提案一次。修正后再次执行确定性校验和 Judge。修正不能调用工具、重新检索、绕过风险或澄清轮数上限；计入原有最多 8 步和总超时预算。预算不足会提前安全失败。Judge 最多两次，无 SDK 自动重试。

二次 FAIL、Judge 超时/无效 JSON/结果矛盾/违规原文不存在、修正结构无效或确定性校验失败，均不创建待审核提案或审核中断，工单保持原状态；运行记为 failed。Judge 故障不当作语义 FAIL 重生成。

## 职责划分

确定性代码保留 schema、risk_flags → escalate、证据 ID、逐字引用与来源匹配、工具白名单/参数/执行次数、来源正文验证和状态事务。`validate_questions()` 只拒绝列表中完全重复的字符串；重复询问已有事实交给 Judge。原来的操作关键词和回复声明正则已移除。

Judge 只输出 `passed` 与 `violations[{type,text,reason}]`：

| 类型 | 判定边界 |
| --- | --- |
| operation_in_clarification | 追问中的新操作要求；否定操作、未回答的历史经历询问允许 |
| repeated_known_fact | 重复询问客户已明确提供的事实；不以理解摘要补造事实 |
| false_status_claim | 声称系统已完成实际未执行的动作；成功只读工具只能证明相应读取 |
| unsupported_commitment | 无依据保证人工/外部人员未来行动；建议人工核查允许 |

“不要做任何修改”“之前是否停用过代理”不因操作词被拦截；“请停用代理后重试”应判违规。“建议人工核查”允许，“届时会由人工确认恢复范围”应判违规。这些是提示词定义与离线回归样本，尚无真实 Judge 实测结论。

输入显式选取 `subject`、结构化 `messages`、完整 `proposal`、实际 `tool_calls` 和系统能力边界。客户事实只来自 `subject` 与 `role=customer` 的消息；support 消息仅作为对话上下文。不会序列化整个评测对象，不传 M4 label、expected_action 或 adjudication。Judge 不拥有工具，也不承担证据真实性校验。

## 审计与失败

使用原有 `ProcessingResult.usage` JSON 保存 `semantic_judge` 数组，每次包含 proposal、判定结果、模型、协议、耗时和可获得的 token 用量。失败另记 `guardrail_failure`，工具执行记录仍只包含真实工具轨迹。

主要错误码为 `semantic_guardrail_failure`（二次拒绝）、`semantic_judge_error`（无法检查）、`guardrail_repair_failed`（修正失败）、`guardrail_repair_invalid`（绕过输入风险）、`guardrail_step_limit`（无修正步数）。API 错误摘要不回显提供商异常内容。已拒绝草稿仅留作审计，不作为可审核/可发布 proposal。

模型元数据记录 `decision_protocol=semantic-guardrail-decision-v1`、`judge_protocol=semantic-guardrail-v1`、两个模型和 `max_guardrail_retries=1`。提示词改变会使旧 Decision 精确缓存不匹配。注入 `decision_fn` 的旧缓存/验收入口必须显式提供 `judge_fn(state, proposal, timeout, usage)`，否则安全失败，绝不自动增加旧额度外的 Judge 付费调用。适配器的实际模型必须与配置一致；离线替身仅用于模拟验证。

后续真实验收应新建独立 run/输出目录，记录协议、模型、样本、两轮提案和 Judge 判定，并单独授权及记录 Judge 和重生成额度。不要覆盖 `docs/m4-*` 的冻结报告及原始评测文件。此次没有启动新一轮真实质量评测。

## 复现验证

```powershell
uv run --no-sync pytest tests -m 'not integration' -q -p no:cacheprovider
$env:TICKETMIND_RUN_DB_TESTS='1'
uv run --no-sync pytest tests/integration/test_reply_claims_http.py -q -p no:cacheprovider
```

数据库测试使用已有连接创建并清理 UUID 隔离 schema，验证实际 HTTP、事务及 PostgreSQL checkpointer。Decision、Judge 和检索为本地替身，不联系模型提供商。Windows 若默认 pytest 临时目录不可访问，可用 `--basetemp` 指定新的可写临时目录。

2026-09-18 实测：全量回归先得到 233 passed / 3 failed / 2 skipped；3 个失败来自旧验收测试未显式提供 Judge 替身，补齐后与新增边界测试一起定向复测，29 passed。合并去重后的已通过覆盖为 240 项（147 项单元/替身、93 项真实 PostgreSQL 流程），2 项真实 Milvus 测试未启用。专门的 Semantic Judge HTTP/数据库测试 8 项通过，覆盖直接通过、修正后通过、二次拒绝和 Judge 故障，以及幂等、审核恢复和失败持久化。未运行真实 Decision/Judge 质量评测；新增付费调用为 0。
