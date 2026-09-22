# 核心代码修改报告 · 2026-09-22

本次范围：工单、Agent 输入、人工审核与接管、运行恢复、知识发布和检索，以及对应工作台入口。不修改阶段验收脚本和测试文件。

## 已实现的修改

| 问题 | 修改后的行为 | 使用入口 |
| --- | --- | --- |
| 工单可以创建但超出 Agent 输入限制 | 新建与追加消息共享 8000 字符上限；Agent 工单上下文（标题加全部消息）上限为 32000 字符。创建运行前检查，超限返回明确错误，不创建运行、不调用模型、不截断原始内容 | 新建工单；工单操作中的处理按钮 |
| 编辑回复后仍按原动作流转 | 新编辑审核同时要求 final_action，可选择解决建议、追问或转人工；最终动作单独保存，原始提案与 action 保留。澄清轮数按实际应用的最终动作计算 | 运行与审核 → 编辑后批准 → 最终动作 |
| Agent 失败后无法独立转人工 | reviewer 可直接接管 open / awaiting_customer 工单，记录原因、递增版本并使旧提案失效，不依赖检索或模型。resolved 和 running 工单不能直接接管 | 工单操作 → 人工接管 |
| 结果落库失败只能依赖重启恢复 | 新增恢复入口和审计。活动请求注册表阻止恢复仍在执行的请求；请求结束后，从检查点恢复待审结果，没有结果则标记 failed；已有未应用审核则保留原审核，交由原请求重试。恢复不调用模型、不发布回复 | 运行与审核 → 恢复运行状态 |
| 完整会话直接变成知识，难以提炼且可能超长 | 新发布必须由人工整理问题、适用条件、最终步骤、验证结果；批准前检查渲染正文不超过 16384 UTF-8 字节。完整会话保留审计，检索和详情工具只提供整理后的文章 | 已解决工单 → 知识沉淀 → 整理四项内容 → 批准发布 |
| BM25 发布依赖文档向量 | 独立 BM25 文本集合可先发布；Dense 就绪状态和失败原因独立记录。HTTP 不新增付费 Embedding。Hybrid 合并 BM25 与已就绪 Dense 来源；无 Dense 来源时明确跳过该通道，也不请求查询向量 | 工单操作 → 本次检索模式；知识卡片的通道就绪状态 |

附带修正：工作台只有在最新消息为客户消息、没有待审结果且输入未超限时才提供自动处理；同一次运行只读取一次 runner metadata。

## 数据与一致性

新增迁移 `d91e6b72a430`，基于 `c3f1a7d4e902`：

- `processing_reviews.final_action`：人工最终动作。历史审核为 NULL，按原动作解释，原 key 和请求哈希兼容。
- `processing_recoveries`：恢复请求的身份、幂等 key、请求哈希、前后运行状态及时间。
- `knowledge_datasets.bm25_collection_name / bm25_ready`：独立文本索引位置与完整构建状态。
- `knowledge_cases.dense_indexed_hash / dense_index_error`：Dense 是否对应当前内容，以及未就绪原因。

已有 active 知识原本通过联合索引校验，迁移将其 indexed_hash 回填为 dense_indexed_hash。旧联合集合保持可读；第一次需要独立文本索引时，在同步锁内从 PostgreSQL 分批复制所有 active 知识正文，全部读回成功后才切换集合指针，不生成向量。集合丢失后的重建会先持久化未就绪状态，避免中途失败后把部分索引当成完整索引。

新知识的 active 表示 BM25 正文索引已就绪，不再意味着 Dense 也就绪；调用方应读取 `retrieval_ready.bm25 / dense`。缺向量缓存时记录 `dense_index_error=embedding_cache_missing`，仍允许 BM25 发布。重试 Dense 时不会先把 active 的 BM25 知识置为 pending。

Hybrid 允许知识只参加 BM25 分支；有 Dense 来源时仍按原有 RRF 融合。已就绪通道的实际服务错误不会伪装为成功。纯 Dense 且无就绪向量时返回 `dense_index_not_ready`。创建运行可通过 retrieval_mode 选择模式，不传时使用服务默认值。

## 启用与操作

本次只编写迁移，未执行迁移、启动服务或重建索引。更新代码后需要先在 app 目录执行：

```powershell
uv run --no-sync alembic upgrade head
```

再启动 API 与工作台。原日常启动器已有 migration 步骤；不要让未升级的数据库直接承载新版本 API。

新增接口：

- `POST /tickets/{id}/escalate`：reason、expected_version。
- `POST /tickets/{id}/runs/{run}/recover`：expected_version。
- 编辑审核：decision=edit、edited_reply、comment、final_action、expected_version。
- 新知识批准：expected_version 和 article，其中包含 problem、applicability、solution、verification。

接口仍要求 Bearer、Idempotency-Key 与 expected_version；接管、恢复和知识发布仅 reviewer 可用。解决建议依然不会关闭工单，关闭仍由人工单独确认。

## 检查与限制

- 按用户要求，未运行或编写测试，未调用模型，未启动服务，未连接数据库或 Milvus 执行验证。
- 对 77 个核心源码与迁移 Python 文件进行了 AST 语法解析，全部通过；`git diff --check` 未发现补丁空白错误或冲突标记。这不证明迁移、并发、Milvus 或 UI 运行正确。
- 新契约（编辑审核需最终动作、发布需整理正文、active 不再等于 Dense 就绪）尚未运行验收；旧测试和客户端后续需要按新契约核对。
- 已发布历史知识正文不自动重写；本次提供发布前编辑，未增加发布后的知识版本编辑功能。
- 恢复保护沿用单实例、单 worker 约束，没有新增后台任务系统。
- 工单上下文采用字符预算，不等同于模型 token 上限；检索证据仍受既有 top-k 和单篇字节限制。
- 知识仍发布到 production-v1。检索库范围不因切换 BM25/Hybrid 改变；冻结合成库不会自动混入新工单知识，侧栏显示当前检索库。
- 补齐文档向量仍需管理员同步流程及另行授权的预算；本次没有付费调用。
