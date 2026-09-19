# TicketMind 真实链路人工验收夹具（仅合成数据）

此目录是 **2026-09 独立测试数据**，并非真实客户历史、真实产品事实或已验收模型指标。不覆盖 `data/synthetic/v2`、`data/synthetic/m4` 或生产知识库。不要把这里的历史结论泛化到外部真实 SaaS 产品。

| 文件 | 用途 |
| --- | --- |
| `historical_cases.jsonl` | 10 条完整、已解决的**虚构历史工单**；只作为本轮 seed 输入，不直接传给 Agent |
| `test_tickets.jsonl` | 21 个待创建的**新工单**；手工按 `input` 四字段分别录入，不把其他字段作为工单内容 |
| `review_checks.jsonl` | 与工单 ID 配对的人类审查草案；**不输入 Agent、检索向量或 Judge**，非独立审核过的准确率金标准 |
| `customer_followups.jsonl` | 用已有工单追加客户消息，检查重新处理和证据适用性 |
| `validate_suite.py` | 只检查数据结构和源 ID，不连数据库或外部模型 |

## 0. 下载与离线检查

在本仓库根目录、Windows PowerShell 中执行：

```powershell
git pull --ff-only
uv sync --locked
uv run --no-sync python data/synthetic/live_e2e_v1/validate_suite.py
```

预期打印 10 historical / 21 tickets / 21 review checks / 1 follow-up。这里只验证静态数据，不意味着线上真实链路通过。先复制仓库根目录的 `env.example` 为未追踪的 `.env`（若已有则保留），配置 PostgreSQL、Milvus、DashScope 业务空间凭据，并运行 `uv run --no-sync python scripts/configure_local_auth.py`。勿提交 `.env`、凭据和调用台账。

## 1. 用独立数据集初始化真实 PostgreSQL + Milvus

**不要使用 `--demo`**：那会把 Agent/检索换成确定性替身。需要 Python 3.12、uv、可连接的 PostgreSQL 和 Milvus；先启动数据库、Milvus（已有容器可复用）。确保当前 PowerShell 工作目录是仓库根目录。

```powershell
$env:TICKETMIND_CORPUS_PATH = (Resolve-Path .\data\synthetic\live_e2e_v1\historical_cases.jsonl).Path
uv run --no-sync alembic upgrade head
$seedOutput = uv run --no-sync python scripts/knowledge.py seed
$seedOutput
$dataset = ($seedOutput | Select-Object -Last 1 | ConvertFrom-Json).dataset_version
$dataset
```

`seed` 会把 10 条历史案例写入 PostgreSQL 中新建的、内容哈希绑定的 **synthetic 数据集**；它不会自动写入 Milvus，也不会调用模型。若有错误，先检查迁移、DB 连接以及权限。旧 `synthetic-v2-...` 和 `production-v1` 保持独立。

**必须建立索引**：当前 `knowledge.py reconcile` 的新文档入索引流程同时要求 1024 维 Dense 向量与 Milvus BM25 字段；即使运行时选择 BM25，首次建立本批新知识的索引也不能跳过文档 Embedding。对全新内容、无完全一致缓存的情形，本批最多需要 **10 次新文档向量请求尝试**；实际计费取决于供应商。执行下面的命令前，确认已同意这批外部调用及费用：

```powershell
uv run --no-sync python scripts/knowledge.py reconcile --dataset $dataset --embedding-budget 10 --ledger data/cache/live_e2e_v1/embedding_attempts.json
```

该命令保存累计尝试台账。**不要删除或重命名台账以规避额度**；若某次失败已消耗尝试额度，先查看该次状态和原因，再决定是否另行授权额外尝试。重跑时默认预算并不会自动增加。如果已存在相同文档精确向量缓存，`reconcile --dataset $dataset` 可以零新增向量请求完成。

成功标准：输出的本批 10 条知识全部是 `active`，每条 `index_error` 为 `null`，且 `orphans_removed` 合理。不能仅根据 seed 创建条数判断检索已经可用。

## 2. 启动真实工作台

继续在**同一个 PowerShell 会话**内：

```powershell
$env:TICKETMIND_KNOWLEDGE_DATASET = $dataset
$env:TICKETMIND_RETRIEVAL_MODE = "bm25"
uv run --no-sync python scripts/start_local.py --skip-infra
```

若本机 Milvus 尚未运行，请先启动它，或去掉 `--skip-infra` 让项目启动器尝试启动仓库 Compose。工作台默认访问 http://127.0.0.1:8501，正式 API 默认 http://127.0.0.1:8000。用 `.env` 中自己配置的 reviewer/operator Bearer token 登录，看到 `live` 模式再开始；**不要把演示模式的替身输出当真实测试结果**。

使用 `bm25` 先验证 PG→Milvus→Agent→Judge→reviewer→PG 的最少外部调用路径：在这个模式下查询检索不使用新的查询 Embedding，**但每次处理仍可能发起真实 Decision、Judge，必要时进行修正及额外检索**，因此不属于免费演示。要验证 Dense/Hybrid，换用 `$env:TICKETMIND_RETRIEVAL_MODE = "hybrid"` 并重启真实 API/UI；新查询会额外调用查询 Embedding。处理 21 条工单前先以少量样本检查模型成本和超时，不要误以为测试文件会限制正式 API 的付费次数。

## 3. 最小完整路径（先只做 01、17、18、21）

1. 从 `test_tickets.jsonl` 找到 `SYN-LIVE-TEST-01`，只把 `input.subject/body/channel/requester_role` 录入工作台“新建工单”，创建并进入“工单操作 → 处理最新客户消息”。
2. 运行结束后必须查看 `run_status`。只有 `waiting_review` 才表示模型提案待审；`failed` 的 `error_code`、`usage`、`tool_calls` 应保留作问题定位。检查“引用与检索证据”中的 `corpus_version` 是否等于上面的 `$dataset`、相关 `source_id` 是否出现、证据正文是否真的支持建议。**最相关的检索候选不等于根因已成立。**
3. 由 reviewer 人工核对后批准或编辑批准；核对原始 `proposal.reply` 与实际消息、`published_message_id`、工单版本的变化。建议解决方案发布后工单通常仍为 `open`，需要 reviewer 明确关闭；请勿让 Agent 输出的“已关闭”等表述代替实际状态。
4. 再做 `SYN-LIVE-TEST-17`（信息不足的追问）和 `SYN-LIVE-TEST-18`（安全事件转人工）；注意追问不能让客户执行新操作，转人工提案不能谎称已经外部派单。
5. 做 `SYN-LIVE-TEST-21`，若首轮建议追问，由 reviewer 批准；工单进入 `awaiting_customer`。复制 `customer_followups.jsonl` 对应 `body`，用“客户补充”消息追加，刷新后再“处理最新客户消息”，观察是否利用新证据、避免重复询问。若首轮已是其他动作，记录真实结果，不要强行假造追问流程。

再按需要测试其余 17 条正例、反例、风险和 prompt injection。每一条新工单使用新的 UI 创建操作；审核草案仅供人类在运行后查看，**不复制进 Agent 的 subject、body 或任何模型上下文**。对于 `review_checks.jsonl` 中 `expected_action = null` 的反例，重点检查是否错误套用旧历史原因，而不是强制某个唯一动作。

## 4. 验收维度与已知界限

- **数据/索引**：10 条知识状态都 active；实际运行的 corpus_version 是本批新 dataset；来源 ID、引用原文和知识详情相互一致。
- **决策/护栏**：关注正例能否得到有证据的处理建议，反例是否承认条件不符，缺信息是否只问现有事实，高风险是否进入人工路径，注入文本是否被当成业务数据。
- **业务闭环**：审核前绝不发布回复；审核后只保存本地消息；追问后客户补充可再次运行；人工明确关闭后才是 resolved。
- **知识回写（可选且需要另行授权新向量）**：选一个不含敏感信息的测试工单，人工处理并明确关闭；在工作台审阅并批准“Publish to Knowledge Base”。若返回 `index_failed / embedding_cache_missing`，这是没有授权新文档向量的预期结果，按项目 `docs/knowledge-writeback.md` 中的受控 `production-v1` 命令为该 TICKET-UUID 额外生成向量、reconcile，待 active 后再用 `TICKETMIND_KNOWLEDGE_DATASET=production-v1` 的独立真实实例检验后续检索。该路径写入的是**生产命名空间的合成测试知识**，不要在包含真实客户数据的生产库中做这一实验。
- **边界**：工作台没有自动上传整个测试 JSONL 的入口；这些文件是逐条手工录入夹具。测试数据能覆盖主要业务分支，但不能证明真实环境下的总体准确率、SLA 或全部并发/恢复行为。

## 5. 诊断与恢复

从根目录单独执行零模型回归：`uv run --no-sync python -m pytest -q`。如有独立测试数据库权限，可按仓库 README 的 `TICKETMIND_RUN_DB_TESTS` / `TICKETMIND_RUN_MILVUS_TESTS` 开关运行真实服务集成测试，测试中的 Agent 可能仍是替身；这与本目录的真实模型人工验收是两项独立工作。

常见失败：`knowledge_dataset_missing` 表示未正确选择/seed；`retrieval_collection_missing` 表示索引尚未创建；`embedding_cache_missing` 表示待索引文档无精确向量；`semantic_judge_error` 表示 Judge 未完成有效审查；`agent_execution_failed` 需要结合运行工具记录和服务日志继续定位。不要为了通过测试去清空旧数据库、重置调用台账或把生产数据集改成当前合成数据集。
