# Knowledge 存储重构与 Write-back

日期：2026-09-17。沿用单 Agent、模块化单体和既有依赖；没有新增模型调用、reranker、后台队列或分布式事务。

## 架构与两条链路

```text
JSONL seed → PostgreSQL KnowledgeCase → 精确文档向量缓存 → Milvus
查询 → Dense / BM25 / Hybrid + RRF → source_id / score / rank
     → 一次 PG 批量读取 → 正文、metadata、证据快照 → Agent

已解决 Ticket → 确定性候选全文 → reviewer 明确批准
     → KnowledgeCase pending_index → 缓存向量 / 显式授权生成
     → Milvus upsert + 强一致读回 → active → 后续工单检索
```

- `knowledge/models.py`、`seed.py`、`repository.py`、`service.py`：权威内容、冻结导入、批量读取、人工批准和操作审计。
- `knowledge/index.py`、`sync.py`：派生索引、精确向量缓存、失败状态和显式修复。
- `retrieval/service.py`：正式路径只取索引身份与分数，RRF 后一次批量 hydration，保留 Milvus 排名。各通道候选、耗时和不一致项继续保存到工具审计。
- `agent/runtime.py` 默认持有 `KnowledgeStore` 和 Session 工厂，不持有全量案例；`get_case_detail` 经 Store 单条读 PG。模型没有数据库连接、SQL 或写工具。
- `api/routes/knowledge.py` 和 `/sources`、`workbench/app.py`：可信身份、版本、幂等标识与 HTTP 工作台。

Milvus 新生产集合仅保存 source_id、corpus_version、content_hash、embedding、BM25 派生文本与 sparse。BM25 必须保存可分词文本，这是可重建的索引材料；Agent 从不将它当权威正文返回。旧固定评测集合保留原 `text` 字段和向量，以兼容历史实验。

## 数据库与版本

Alembic **b812ce904a61** 基于 **9c42d71ab203** 新增：

| 表 | 关键字段与约束 |
| --- | --- |
| knowledge_datasets | version 主键；kind=synthetic/production；manifest；collection_name 唯一；创建时间 |
| knowledge_cases | (dataset_version, source_id) 联合主键；标题、问题、完整知识正文、结构化来源、metadata；source_type；source_ticket_id 外键且唯一；内容 hash、revision；状态 version；reviewer/approved_at；index_error/indexed_hash/indexed_at；retired_at 与创建/更新时间 |
| knowledge_operations | resource/actor/operation/Idempotency-Key 联合唯一；请求 hash 和时间，用于批准、重试、停用审计 |
| knowledge_embeddings | 按 provider/region/workspace/model/dimension/document text 计算 fingerprint；保存完整身份与向量；成功向量先提交，再写索引 |

知识约束要求状态在 `pending_index / active / index_failed / retired` 内、版本为正、active 有匹配 indexed_hash，ticket 来源必须有原工单及人工批准人/时间。`(dataset_version, status)` 有索引。迁移不读取模型、不连接 Milvus、不修改旧表；空表可降级，有数据时拒绝有损降级。业务表不在应用启动时创建。

`corpus_version` 不删除：固定 synthetic-v2 完整内容 hash 保持不变，用作 dataset_version；旧 ProcessingResult、评测标签 hash、提示词缓存和集合 manifest 仍可解释。生产用 `production-v1` 表示独立知识命名空间；每条证据额外保存 knowledge_revision、content_hash、synthetic、metadata 与完整正文。它不伪装成生产知识总快照版本。

本阶段正文创建后不提供原地编辑，revision 为 1；状态更新只递增 version，不改正文。已停用来源仍能经版本化 `/sources` 审计，不能进入正常检索。后续若新增知识编辑，必须增加版本保留设计，不能覆盖旧 revision。旧运行的证据快照保持原样。

## 人工批准与工作台

resolved 工单的候选是从已发布消息按 sequence_number 和作者标签构造的只读视图，包括客户更新、已批准回复、人工回复及关闭说明；不读取未批准的模型草稿，也不再调用 LLM 总结。GET 不写数据库。采用派生候选是因为 resolved 是终态，避免额外候选表和无意义的自动写入。

reviewer 在工单底部查看全文、勾选确认、点击 **Publish to Knowledge Base**，再确认固定请求。批准事务锁住原 Ticket，核对 expected_version，保存固定 `TICKET-{UUID}` 来源及人工审核；同一 Ticket 最多一条知识。工单本身不再次关闭或追加消息。

| 接口 | 权限 / 版本 |
| --- | --- |
| GET /tickets/{ticket}/knowledge | operator/reviewer；eligible、candidate 或 knowledge |
| POST /tickets/{ticket}/knowledge/approve | reviewer；expected_version 是 Ticket.version |
| GET /knowledge/{dataset}/{source} | operator/reviewer；全文及审核、索引状态 |
| POST /knowledge/{dataset}/{source}/retry | reviewer；expected_version 是 KnowledgeCase.version |
| POST /knowledge/{dataset}/{source}/retire | reviewer；版本同上，先在 PG 停用再删除索引 |
| GET /sources/{source}?corpus_version={dataset} | PG 的不可变版本来源；缺失返回 404，无 JSONL fallback |

所有 POST 需要 Idempotency-Key，同 key 改请求返回 409；同 key 恢复不重复批准。同 Ticket 不同 key 再次批准也返回已有案例，审核人/时间不被覆盖。重试只作用于已有知识。operator 没有发布按钮，绕过页面调用 API 也返回 403。工作台只走 HTTP，不访问 PG、Milvus 或 Agent。

## 一致性与恢复

| 情况 | 行为 |
| --- | --- |
| 批准已提交，索引尚未写 | pending_index；可显式 reconcile |
| 缺向量且预算为零 | index_failed / embedding_cache_missing；不调用模型 |
| Embedding 成功，Milvus 失败 | 向量已单独提交；失败状态可见，下次复用向量 |
| Milvus 成功，PG 状态提交失败 | pending/failed；下次先读回同 ID/hash，确认后激活，不重复生成向量 |
| active 的 Milvus 行丢失 | `--repair-active` 检查并用 PG 缓存重建 |
| Milvus 有 / PG 无 | 跳过，记录 missing_postgres_source 日志及运行工具审计，不返回虚假正文 |
| PG 停用 / Milvus 删除失败 | 仍是 retired，记录删除错误；hydration 立即过滤，reconcile 再删除 |
| 停用与索引网络请求并发 | 网络返回后的版本检查不能覆盖较新的停用状态；后续修复残留索引 |

同步以数据库/schema/dataset 作用域的 advisory lock 串行执行。它持有专用连接，但网络期间不持有数据库事务或行锁；进程断开时 PG 释放锁。并发同步返回 `knowledge_sync_busy`，不会启动第二次外部操作。upsert 使用稳定 ID，读回确认后才设 active。

不一致项包含 source_id、dataset_version 和稳定错误码，失败不泄露上游原始异常。PG 全部不可写时无法保证错误立即落库，原 pending/旧状态及应用日志保留；恢复后显式修复。正常 API 检索不主动扫描整个索引，也不能发现未被查询命中的孤儿项。

## 初始化与运维命令

在项目根目录运行，使用原 `.env`，不重写凭据：

```powershell
uv run --no-sync alembic upgrade head
uv run --no-sync python scripts/knowledge.py seed
uv run --no-sync python scripts/knowledge.py import-cache
uv run --no-sync python scripts/knowledge.py reconcile --dataset production-v1
```

seed 可重复执行；相同内容不会重复插入或复活 retired，也不会调用 embedding。不同合成内容保留为另一冻结数据集，不覆盖旧数据。import-cache 验证现有 `data/cache/embeddings` 的完整精确身份后导入 PG；缺文件即失败。生产与合成索引分离，生产初始为空。

固定合成集：

```powershell
$dataset = 'synthetic-v2-e5b5a59a7e1481ad3b095d518772354155d891e51ad2a367cf5f7be26540228f'
uv run --no-sync python scripts/knowledge.py reconcile --dataset $dataset
$env:TICKETMIND_KNOWLEDGE_DATASET = $dataset
uv run --no-sync python scripts/start_local.py --skip-infra
```

恢复生产模式，设置 `TICKETMIND_KNOWLEDGE_DATASET=production-v1`。生产新增知识不改变数据集名、分析器或集合，也不需要重启。变更整个数据集选择仍是服务配置变更。

修复入口默认扫描最多 100 条 pending/failed 或删除未完成的 retired，按更新时间轮转；`--limit` 最大 1000：

```powershell
uv run --no-sync python scripts/knowledge.py reconcile --dataset production-v1
uv run --no-sync python scripts/knowledge.py reconcile --dataset production-v1 --repair-active
uv run --no-sync python scripts/knowledge.py reconcile --dataset production-v1 --source-id TICKET-实际UUID
```

HTTP 发布/重试没有付费开关，默认只用缓存。只有取得新的明确授权后，管理员才能执行以下**示例**，它不是当前已授权预算：

```powershell
uv run --no-sync python scripts/knowledge.py reconcile --dataset production-v1 --source-id TICKET-实际UUID --embedding-budget 1 --ledger data/cache/knowledge/approved-batch/attempts.json
```

复用项目的 AttemptLedger 和单次 HTTP transport：发送前持久化记账，累计上限、重复请求指纹及失败尝试均受约束；不自动重试、不能删除台账恢复额度。知识文档只使用 initial_embedding 类别，其他三类额度为零。正预算必须指定台账；缺缓存的零预算请求返回非零退出码。

`serve_m5_demo.py` 的知识索引/向量替身仅在隔离演示明确注入，数据仍通过真实 PG/API 保存；界面已有演示标记。其 active 只说明替身索引状态，不能当作真实 Milvus 验收。

## 验证与限制

最终 **199 passed，0 failed / error / skipped**：原回归 **161** 项，新增 **38** 项；**112** 项逻辑/替身与 **87** 项真实 PostgreSQL。新增部分为知识 PG/API 32、真实 Milvus 2、工作台 HTTP/PG 3、超时预算逻辑 1；保留两条既有依赖弃用警告。逐项数量和本机证据见 [knowledge-verification.json](knowledge-verification.json)，原始 JUnit 在忽略提交的 `data/cache/knowledge/regression.xml`。

额外执行原 M3 **8/8** 真实 HTTP/PG/Milvus 场景，旧集合文本和向量 hash 核对未变；M4 离线预检、M0 图缓存预检、旧 versioned 导入命令均通过。当前 development 数据库已迁移到 b812ce904a61；首次 seed 12、再次新增 0，精确缓存重复导入后无重复向量记录，12 条冻结合成知识 active，生产空集合已初始化。未改写既有工单/运行/审核。

本阶段新增真实付费调用：**LLM 0、Embedding 0、其他 0**。测试中的确定性向量和本机 SDK 故障请求不计为供应商调用。

全套默认测试跳过真实数据库；完整零付费验收命令：

```powershell
$env:TICKETMIND_RUN_DB_TESTS = '1'
$env:TICKETMIND_RUN_MILVUS_TESTS = '1'
uv run --no-sync python -m pytest -q
```

新集成测试覆盖真实 PG 的 seed、缓存幂等、批准权限与并发、索引失败/修复、状态事务失败、停用竞争、批量 hydration、无 JSONL runtime、迁移降级以及默认 HTTP 零预算。工作台测试通过 AppTest → loopback HTTP → PG，索引/向量为明确替身。Milvus 测试使用独立 UUID 集合验证 HTTP 批准→索引→新工单 Agent→PG 证据、三种模式、修复和孤儿过滤；旧固定集合仅只读比较排名和正文。测试结束只清理测试自己的 schema/集合。

真实仍有的限制：

- 新知识正文是完整已发布会话，未做 LLM 摘要或自动去隐私处理；reviewer 必须核对全部内容及适用性。没有案例编辑功能，过长内容（索引上限 16384 UTF-8 bytes）在调用向量前失败，不静默截断。
- 生产固定分析器与旧冻结评测的词典不同；没有新的生产语义质量/排序指标。M4 的状态措辞、追问缺失及未运行样本限制不变。
- 未授权新向量时，新内容可以完成批准但会停在明确失败状态；需要管理员获授权后同步。没有可靠异步队列、自动定时修复或分布式事务。
- 单实例单 worker、单团队预置身份；没有外部邮件或真实客户发布。schema migration/知识初始化在本地开发库执行，不等于生产部署。
- 本阶段未进行新的真实模型决策验收或浏览器人工验收；AppTest 与真实服务测试不能替代模型效果评测。
