# TicketMind 当前进度交接

> 更新于 2026-09-13。保留当前进度、验证边界和后续入口。路径相对 `D:\AnalyzeAgent\app`；本次按用户要求只更新日志，未修改源码、测试或配置，未调用模型。用户明确要求先不做 graph.py，停在检索接图前。

## 1. 协作与项目入口

- 先读 `D:\AnalyzeAgent\AGENT.md`、根目录《TicketMind-TicketAgent-项目立项与技术基线-v1.0.md》，再核对源码。
- 学习模式：每次推进一个可验证模块，先解释业务位置、原理和取舍，再逐段给代码，由用户手动写入；未经明确要求不直接修改源码、测试或配置。
- 已确认的规则不重复询问；真正缺失的信息询问用户。不回到数据库规范优化，不重复已通过的付费模型调用。
- 用户要求直接验证真实流程，不使用 Mock 或人工返回的案例。复用真实模型产生的缓存向量查询真实 Milvus 不属于模拟。
- 用户需要基础概念解释：已讲解节点就是处理步骤对应的函数、state 是随流程传递的处理记录、边规定执行顺序、compile 与 invoke 的区别。后续继续先用直白语言解释，再分段给代码。
- Python 3.12 + uv，项目为 src 布局。Python 命令从 `D:\AnalyzeAgent\app` 执行；模型与 Milvus 应用配置读取该目录 `.env`。

## 2. 已完成：基础、工单与 Agent 理解

- FastAPI `GET /health`、`POST /tickets` 已实现；创建接口用户确认验证通过。
- PostgreSQL + SQLAlchemy + Alembic 已落地；三表为 Ticket、TicketMessage、ProcessingResult。此前迁移验证为 `55ee2375ea43 (head)`，不是本次重新检查结果。
- TicketCreate 只接收 subject、body、channel、requester_role；服务端生成 `TM-` + uuid4.hex，默认 open/P3；首条消息为 customer、序号 1。工单和首条消息同一事务，服务 flush，路由提交成功后返回 201。
- MVP 无 tenant；仅保存脱敏文本与角色（自动脱敏尚未实现）。后续每次 Agent 处理新增 ProcessingResult，不覆盖历史；工单状态为 open/awaiting_customer/resolved/escalated。
- `llm/client.py` 已接千问；`agent/understand.py` 返回 TicketUnderstanding（summary、error_codes、environment），JSON Object 输出后由 Pydantic 校验。
- `agent/graph.py` 当前仍为 `START → understand → END`，build_ticket_graph 仍只接收 settings；两条理解样本和单节点图此前已由用户确认通过。状态已增加检索字段，见第 7 节；不代表检索已接入图。
- HTTP 创建工单与脚本运行图仍未接通。检索函数已写入但未注册到图；无决策节点、业务工单读取、处理结果写库、多轮状态恢复或人工处理流程。

## 3. 已完成：历史语料与模型

- `data/synthetic/v2/`：12 条 historical_cases、16 条 evaluation_cases、16 条独立 evaluation_labels；合成数据，不代表真实产品知识或稳定效果指标。
- 历史案例字段为 source_id、synthetic、request、status=resolved、resolution；resolution 含 summary/root_cause/actions/verification。
- `knowledge/corpus.py`：load_historical_cases 校验语料、重复 ID 和空输入；build_case_text 构造检索文本。已验证读取 12 条。
- 评测标签禁止进入模型输入、检索索引或工具证据。历史语料目前未导入 PostgreSQL。
- 已选定阿里云北京地域，DASHSCOPE_API_KEY、DASHSCOPE_WORKSPACE_ID 已配置，无需重复询问或输出值。
- 文本模型配置默认 qwen3.7-flash；Embedding 已明确改用 text-embedding-v4（覆盖原基线的 Embedding 选型）。
- `retrieval/embeddings.py` 使用 DashScopeEmbeddings：文档 embed_documents、查询 embed_query，1024 维；设置北京业务空间原生 API 地址。现有 max_retries=1，SDK 地址为进程全局配置。

## 4. 已完成：Docker 与 Milvus 部署

- 已核对：Docker 程序 `D:\Docker\Desktop`；WSL 数据设置 `D:\Docker\Data`；实际存在 `disk\docker_data.vhdx` 和 `main\ext4.vhdx`。不再处于“迁移待确认”阶段。
- 已验证 Docker Client/Engine 29.7.2、Desktop 4.90.0、Compose 5.5.1、WSL 2.6.1.0；当时 Engine 可用 4 CPU、约 11.7 GiB 内存。新会话不能据此假定服务此刻运行。
- `infra/milvus/docker-compose.yml` 已落地：Milvus v3.0.1、etcd v3.5.25、MinIO RELEASE.2024-12-18T13-15-44Z，内嵌 Woodpecker。
- `infra/milvus/.env` 指定 DOCKER_VOLUME_DIRECTORY，三服务数据绑定到 `infra/milvus/volumes/{etcd,minio,milvus}`，均在 D 盘。
- 宿主机发布端口仅绑定 127.0.0.1：19530（客户端）、9091（健康/WebUI）、9000/9001（MinIO）；etcd 未发布。当前本地 Milvus 未启用鉴权。
- 用户确认三个容器健康及 `/healthz` 返回 OK、HTTP 200。
- 日常启动：先打开 Docker Desktop，待 Engine 就绪，在 `infra/milvus` 执行 `docker compose up -d`、`docker compose ps -a`；停止用 `docker compose stop`。保留挂载目录即可保留数据，不需每次 pull 或重建集合。
- 找不到 dockerDesktopLinuxEngine 管道曾由 Docker Engine 未就绪引起；先检查 Desktop/Server，不修改 Milvus 配置。

## 5. 已完成：Python 接入、集合与入库

- PyMilvus 固定 3.0.1。`core/config.py` 的 MilvusSettings 前缀已修正为 `TICKETMIND_MILVUS_`；uri 为 http://127.0.0.1:19530，timeout_seconds 默认 10。
- `retrieval/milvus_client.py` 创建客户端；`scripts/check_milvus.py` 列集合验证已由用户确认通过。
- `retrieval/case_collection.py` + `scripts/init_case_collection.py` 已落地，用户确认创建与检查通过。
- 集合 historical_cases_v1：source_id 为 VARCHAR 主键（128 字节）、text 为 VARCHAR（16384 字节）、embedding 为 FLOAT_VECTOR（1024 维）；auto_id=False、动态字段关闭、Strong 一致性；索引 embedding_flat = FLAT + COSINE。
- `knowledge/vector_cache.py`：VectorRecord/VectorCache；根据整批文本、模型等生成指纹，文档向量缓存到 `data/cache/embeddings/`。缺少缓存时仅在 --allow-embedding 下调用模型；先保存缓存，再入库。
- 整批缓存是当前固定 12 条语料的开发辅助；一条文本改变会使整批缓存失效，未实现逐条增量向量化或分批断点续传。缓存无需永久保存，日常查询使用 Milvus 已存向量。
- `scripts/ingest_historical_cases.py` 使用 upsert，随后 get 核对 ID/文本、query count(*) 核对 12 条。用户明确确认“入库成功”；未单独提供第二次幂等导入或重启后只读验证结果，不宣称这两项已验证。
- upsert 不自动删除语料中移除的旧案例；没有更换模型或自动迁移集合逻辑。

## 6. 已完成：Dense 检索最小闭环

- `retrieval/dense.py`：RetrievalHit（source_id/text/score）；search_case_vectors 校验维度、有限数值、非零向量，然后搜索 Milvus，转换候选。实际参数名为 query_vectors（单条向量），top_k 范围 1～100，返回类型已补为 list[RetrievalHit]。
- `scripts/check_dense_retrieval.py` 已落地：分号/逗号不匹配导致 CSV 导入只有一列的查询；embed_query 生成向量，缓存到 `data/cache/queries/csv_separator.json`，后续复用；top_k=3。
- 用户明确确认相关案例 SYN-HIST-V2-002 进入前三且排第一；未提供具体分数。证明单样本最小闭环，不等于整体检索评测通过，也未确定业务相似度阈值。
- **已修正**：client.search 的 `search_param` 已改为本地 SDK 正式参数 `search_params`，返回类型也已补齐；用户回复“完成”，助手已核对源码。未收到修正后的具体检索输出，不补写新的排名或分数结论。
- 本轮只核对源码，未重跑数据库、模型、检索或 pytest。此前最新 pytest 记录为 12 passed（含一条 Starlette/httpx 弃用警告），不覆盖新检索链路或完整数据库/API 集成测试。

## 7. 已写入：检索状态与独立检索函数（尚未运行验证）

- 用户已确认查询采用原始工单标题与正文，不使用理解结果构造查询；格式固定为 `f"标题：{subject}\n\n问题描述：{body}"`。这只是输入格式选择，尚无检索效果提升结论。
- `agent/state.py` 已增加 `retrieval_query: NotRequired[str]`、`retrieval_hits: NotRequired[list[RetrievalHit]]`，并新增 RetrievalUpdate，要求同时返回 query 和 hits 两个对应字段；保留 UnderstandingUpdate。完整字段名为 retrieval_query、retrieval_hits。
- 已解释：TypedDict 是类型契约，不自动执行 Pydantic 式运行时校验；NotRequired 允许字段暂不存在，不自动生成默认值。计划使用普通覆盖更新，不为证据列表配置追加 reducer。
- `agent/retrieve.py` 已由用户写入，2026-09-13 核对：build_retrieval_query 拼接标题和正文；retrieve_ticket 接收 state，以及 embeddings、client、top_k、timeout，调用 embed_query 和已有 search_case_vectors，返回 RetrievalUpdate，不原地修改 state。
- 已修正并核对：导入为 `from langchain_core.embeddings import Embeddings`；Dense 调用使用 `query_vectors=query_vector`；查询格式与已确认方案一致。
- 检索函数只返回证据，不决定 resolve/ask_clarification/escalate，不设置业务分数阈值；空候选返回 []，外部调用异常向上传递。timeout 只传给 Milvus，不控制 Embedding 超时。
- 已说明客户端生命周期：计划由运行脚本创建 Embedding/Milvus 客户端，传入图供节点复用，由脚本 finally 关闭 Milvus；当前检索函数不创建或关闭客户端。尚未新增运行入口。
- 曾给出导入检查和 Mock 验证示例，但没有收到执行结果，助手也未执行；随后用户明确改为真实验证。因此不宣称该模块测试通过，不继续采用模拟验证方案。

## 8. 已确认：真实验证与缓存要求（授权尚未使用）

- 用户明确授权：本次为 `understand → retrieve` 完整流程调用一次理解模型、一次 Embedding，并保存结果供后续复用。无需重复询问这项授权；不是无限重试或反复付费运行的授权。本轮尚未执行这两次调用。
- 现有查询缓存 `data/cache/queries/csv_separator.json` 保存 query、model、1024 维 vector，不保存理解结果或 Milvus 命中结果。原查询为：`上传 CSV 后，预览把姓名和邮箱挤在同一列。文件实际用分号分隔，导入选项目前选的是逗号。`
- 原检查脚本缓存存在时核对查询文本和模型名称；不匹配则报错，匹配则复用向量并重新查询真实 Milvus；缓存不存在时会调用 Embedding。
- 新查询包含标题、字段标签及换行，与旧缓存文本不一致，不能把旧向量当成新查询的真实向量。保留旧缓存，为新输入保存相应结果。
- 新的理解结果缓存、查询向量缓存及复用入口尚未实现。计划理解结果成功后立即保存，Embedding 成功后立即保存，再查询 Milvus，避免后续失败导致重复付费；保存实际输入和模型信息并检查匹配关系。
- 正式执行前先做无付费预检：导入/接口正确、Milvus 可用且集合存在、缓存保存位置可写；核对 Embedding SDK 的 max_retries=1 实际行为，避免超出一次调用授权的自动重试。不要为了预检调用模型。

## 9. 暂停点、恢复入口与剩余范围

1. **用户当前要求先不做 graph.py，仅保存进度。** 对话中已分段给出 graph.py 接图草案，但未写入源码；下次恢复从这里继续，不把草案当成已完成。
2. 草案方向：build_ticket_graph 接收 settings 及 embeddings、client、top_k、timeout；保留 understand_node，新增调用 retrieve_ticket 的 retrieve_node；注册节点并连接 `START → understand → retrieve → END`。客户端仍由外层管理。
3. 现有 `scripts/check_ticket_graph.py` 仍匹配旧图接口。接图后需同步准备真实验证入口和缓存机制，再执行已授权的一次理解模型和一次 Embedding，检查最终状态保留工单、理解结果和检索证据；尚未形成完整可执行方案，不直接运行旧脚本消耗调用。
4. 此后推进 BM25、融合、重排序、证据决策与有限循环。未完成：检索系统评测、风险/置信度阈值、追问上限、工单实际 resolved 条件、HITL、多轮状态与结果持久化、HTTP 与图接通。
5. 延后待办：独立测试库与完整接口/事务测试、查询/追加消息接口、幂等性、异常映射、自动脱敏。ProcessingResult.final_reply 与消息正文重复存储问题在接回复持久化前再处理，不阻塞当前检索主线。
