# TicketMind

当前输出护栏为独立 Semantic Judge：Decision 默认 `glm-5.3`，Judge 默认 `deepseek-v4.1-flash`。最终提案先经过确定性校验，再由 Judge 检查四类语义违规；拒绝后最多修正一次，仍失败则安全退出。配置、审计、离线验证与真实模型验收限制见 [Semantic Judge 新实验](docs/semantic-guardrail-v1.md)。新组合已做真实诊断，但仍有语义误判、协议错误和 Decision 超时/截断，尚未完成质量验收；历史报告保留实际使用的模型，M4 冻结结果未修改。

当前知识架构：**PostgreSQL 保存权威正文，Milvus 提供检索索引，JSONL 仅用于 seed / evaluation**。已解决工单在工作台显示待审候选，经 reviewer 明确批准后形成知识；索引失败可见、可重试，生产新增知识无需重写 JSONL 或重启 Agent。完整设计、初始化和验收见 [Knowledge 交付报告](docs/knowledge-writeback.md)。下文 M0—M5 的模型调用及质量数据保留为历史证据。

首次使用现有开发库，先执行：

```powershell
uv run --no-sync alembic upgrade head
uv run --no-sync python scripts/knowledge.py seed
uv run --no-sync python scripts/knowledge.py import-cache
uv run --no-sync python scripts/knowledge.py reconcile --dataset production-v1
```

`import-cache` 仅导入已有精确向量，缺失即停止；不调用模型。生产知识初始为空，合成 seed 不混入生产检索。固定合成数据集的同步和选择方式见报告。HTTP 发布/重试仅使用缓存；新工单缺向量时返回 `index_failed / embedding_cache_missing`，管理员必须在明确授权后通过带累计台账的命令生成向量。

面向合成 SaaS 技术支持工单的单 Agent 项目。当前 Agent 业务输入已收敛为 `subject + messages[{role, content}]`：身份认证 → 工单与客户消息 → 检索和有界决策 → 独立 Semantic Judge → 持久化暂停 → 人工审核 → 保存回复 → 客服明确关闭。

三类提案都先进入 waiting_review。**生成建议不会关闭工单；发布仅指本地数据库消息，没有接入邮件或外部客服平台。** 本轮 M2 验收收尾已完成：历史三类真实提案及审核、客户补充后真实重检索与建议、用户批准编辑后的业务应用均有证据，工程验证已覆盖审核与进程恢复。

语料为 data/synthetic/v2/ 中的 12 条历史案例和 16 条开发输入，均为合成场景，不代表真实客户效果。M3 已实现 BM25、Dense/BM25/Hybrid 模式与 RRF；首次和再次检索均接入工单处理。真实检索对比、8 个 HTTP/数据库场景及限制见 [M3 交付报告](docs/m3-retrieval.md)。

此前 M2 决策协议 `m2-action-boundaries-v2` 下，glm-5.2用真实新查询向量在Milvus召回006并生成建议，随后精确缓存回放应用用户批准的编辑稿：completed、工单open、版本3→4、消息3→4，重复审核幂等。草稿中过度结论已由人工编辑删除；此前失败样本均保留。实际证据见 [补充验收](docs/m2-followup.md)及[审核单](docs/m2-followup-review.md)。这些是不同阶段/模型的合成样本证据，不代表当前单模型三路径完整回归或总体准确率。

M4 A档已完成：39条新评测输入、33条开发标签补全；51条查询完成Dense/BM25/Hybrid真实对比，6条validation Agent样本动作匹配5/6，3条草稿错误声称已转交。实际新增调用65次。标签和语义结果由用户委托Agent审查，不是独立人工标注；剩余33条Agent输入尚未运行。见[M4报告](docs/m4-evaluation.md)、[标签审阅清单](docs/m4-label-review.md)与[调用记录](docs/m4-call-budget.md)。

M5 新增 Streamlit 工作台：工单列表与消息 → 处理 → 查看提案、证据与工具轨迹 → 人工审核 → 人工回复或明确关闭。M5 当时全量 **161 项测试通过（111 逻辑/替身＋50 真实 PostgreSQL）**；新增 6 个工作台场景经过真实本机 HTTP，Agent 使用替身。知识阶段最终全量为 **199 passed（112 逻辑/替身＋87 真实 PostgreSQL，含真实 Milvus 测试）**，新增付费调用为 0，未重新评定上述 M4 模型质量。

## 先运行工作台

配置根目录 `.env` 的 PostgreSQL 连接后，运行零付费隔离演示：

```powershell
uv sync --locked
uv run --no-sync python scripts/start_local.py --demo
```

打开 `http://127.0.0.1:8501`，从启动日志指向的本地临时凭据文件取 reviewer_token 登录。演示使用独立 schema 与确定性 Agent/检索替身，正常退出清理本次数据；它展示业务流程，不代表真实模型效果。

正式模式使用 `uv run --no-sync python scripts/start_local.py`，启动前配置模型、身份和现有 Milvus，并按下文初始化语料。该模式的处理操作可能付费，启动器本身不触发模型请求。数据目录与已有 `.env` 保留。

- [启动说明与 4 分钟演示](docs/m5-demo.md)
- [架构与 API](docs/architecture.md)
- [M5 验收与限制](docs/m5-workbench.md)
- [上游来源、贡献范围与简历措辞](docs/sources-and-contributions.md)

历史验收资料中仍可能包含旧 `understanding` 缓存和调用记录；它们仅作为冻结实验来源保留，不属于当前 runtime。旧验收适配器没有 Judge 调用额度，新协议下需显式接入 Judge 适配器并使用独立新 run，不可直接复用旧额度启动真实评测。历史端点测试中 `glm-5.2` 可用，`glm5.2` 返回 NotFoundError；失败请求仍计入历史台账。

## 安装与配置

需要 Python 3.12、uv、PostgreSQL 和现有本地 Milvus。从仓库目录执行：

```powershell
Set-Location D:\AnalyzeAgent\app
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item env.example .env }
uv run --no-sync python scripts/configure_local_auth.py
```

保留已有 .env。身份脚本仅填充缺失凭据，不输出或替换现有值；两个身份应独立分配。

| 配置 | 用途 |
| --- | --- |
| TICKETMIND_DATABASE_URL | postgresql+psycopg://...；账号需业务迁移及检查点建表权限 |
| DASHSCOPE_API_KEY、DASHSCOPE_WORKSPACE_ID | 阿里云北京业务空间；预检不证明凭据有效 |
| TICKETMIND_DECISION_MODEL | 默认 glm-5.3，用于决策及唯一一次护栏修正 |
| TICKETMIND_JUDGE_MODEL | 默认 deepseek-v4.1-flash，必须与 Decision 不同 |
| TICKETMIND_EMBEDDING_MODEL | 现有集合固定 text-embedding-v4、1024 维 |
| TICKETMIND_MILVUS_URI、TICKETMIND_MILVUS_TIMEOUT_SECONDS | 默认本地 http://127.0.0.1:19530、10 秒 |
| TICKETMIND_OPERATOR_TOKEN、TICKETMIND_REVIEWER_TOKEN | 独立 Bearer 凭据，至少 32 个非空白 ASCII 字符 |
| TICKETMIND_OPERATOR_ID、TICKETMIND_REVIEWER_ID | 服务端预置身份，默认 operator/reviewer，必须不同 |
| TICKETMIND_AGENT_VERSION | 默认 ticketmind-m3；旧环境显式设置 m1/m2 时应更新这个非密钥项 |
| TICKETMIND_RETRIEVAL_MODE、TICKETMIND_RETRIEVAL_TOP_K | dense / bm25 / hybrid；默认 dense、3 条，M3 对比使用 5 条 |
| TICKETMIND_RETRIEVAL_CANDIDATE_K、TICKETMIND_RETRIEVAL_RRF_K | Hybrid 每路候选默认 20（至少 top_k）；RRF k 默认 60 |
| TICKETMIND_PROCESSING_TIMEOUT_SECONDS | 默认 90 秒，阶段检查/SDK 超时，非进程硬截止 |
| TICKETMIND_MAX_SEARCH_ROUNDS | 检索含首次最多 2 轮，可调低 |
| TICKETMIND_MAX_CASE_DETAILS | 最多 2 个不同候选详情，可调低或设 0 |
| TICKETMIND_MAX_AGENT_STEPS | 首次检索、每次决策（含护栏修正）和每次工具调用共同受总步数预算约束，最多 8 步；Judge 最多两次，另受总超时约束 |
| TICKETMIND_MAX_CLARIFICATION_ROUNDS | 最多 2 轮，按成功应用的追问审核计数 |
| TICKETMIND_CORPUS_PATH | 仅 seed / evaluation；可选，默认项目内 v2 历史案例 |
| TICKETMIND_KNOWLEDGE_DATASET | 正式 Agent 的 PostgreSQL 数据集；默认 production-v1，可显式选择冻结合成版本 |

模型地址由业务空间拼接为 https://&lt;workspace&gt;.cn-beijing.maas.aliyuncs.com/compatible-mode/v1（Decision/Judge）和 /api/v1（Embedding）。本地 Milvus 适配尚无鉴权，不直接暴露公网。

## 数据库与启动

```powershell
uv run --no-sync alembic upgrade head
uv run --no-sync alembic current
uv run --no-sync uvicorn ticketmind.main:app --host 127.0.0.1 --port 8000 --workers 1
```

最新业务迁移为 **b812ce904a61**，在 M2 的 **9c42d71ab203** 之上新增知识数据集、知识案例、操作审计和精确向量缓存四张表。旧工单、运行、审核不改写；有知识数据时拒绝有损降级。M2 的审核表、消息审计、发布消息关联和 cancelled 状态全部保留。

启动使用独立连接池初始化 PostgreSQL checkpointer 的 checkpoint_* / checkpoints 表，与业务表位于相同数据库/schema。检查点由 saver.setup 管理，业务表由 Alembic 管理。仅新增 langgraph-checkpoint-postgres 3.1.2 及其 psycopg-pool 3.3.1；保留 LangGraph 1.2.11 和其余既有版本。

**一个实例、一个 worker、一个团队。** 会话级 advisory lock 拒绝同一数据库/schema 的第二个 API 实例，避免错误恢复仍在执行的运行。没有分布式接管或可靠队列，不使用多 worker。

打开 [Swagger](http://127.0.0.1:8000/docs)，在 Authorize 中填入本地凭据。[health](http://127.0.0.1:8000/health) 仅表示存活。requester_role 不参与授权。

## API 流程

写操作均要求 Idempotency-Key。相同身份、操作类型、资源和 key 的同内容请求返回已有结果；内容不同返回 409。保留 key 和完整请求体以便重试。运行/审核返回 201 只表示建立记录，必须读取 run_status。

| 方法与路径 | 权限及输入 |
| --- | --- |
| GET /auth/me | Bearer；返回服务端身份、权限与 live/synthetic_demo 模式，不返回凭据 |
| POST /tickets | operator/reviewer；subject、body、channel、requester_role |
| GET /tickets | status、limit、offset；稳定分页 |
| GET /tickets/{ticket_id} | 版本、有序消息、最新运行 |
| POST /tickets/{ticket_id}/messages | body、kind、expected_version；kind 为 customer_update 或 human_reply，后者仅 reviewer |
| POST /tickets/{ticket_id}/runs | trigger_message_id、expected_version；open 工单最新客户消息 |
| GET /tickets/{ticket_id}/runs | 历史运行分页 |
| GET /tickets/{ticket_id}/runs/{run_id} | 原提案、证据、工具记录、配置、usage、审核与错误 |
| POST /tickets/{ticket_id}/runs/{run_id}/review | 仅 reviewer；decision、expected_version、可选 edited_reply/comment |
| POST /tickets/{ticket_id}/close | 仅 reviewer；expected_version、reason，保存关闭审计 |
| GET /sources/{source_id}?corpus_version=... | 从 PG 读取对应版本完整来源；含停用来源供审计，无此版本返回 404 |
| GET /tickets/{ticket_id}/knowledge | 待审候选全文或已沉淀知识状态 |
| POST /tickets/{ticket_id}/knowledge/approve | reviewer；expected_version 是工单版本，批准并尝试缓存索引 |
| GET /knowledge/{dataset}/{source_id} | 知识正文、审核来源、版本、索引状态与错误 |
| POST /knowledge/{dataset}/{source_id}/retry 或 /retire | reviewer；expected_version 是知识状态版本 |

创建工单后查询详情，取得 version 和最新消息 id，再创建运行。**新运行可能调用付费模型，只在已授权预算内执行。** 正式 API 不启用开发缓存。

待审核时使用 reviewer 身份提交以下之一；示例 version 应换成实际值：

```json
{"decision":"approve","expected_version":1}
```

```json
{"decision":"edit","expected_version":1,"edited_reply":"请提供当前代理配置和完整错误响应。","comment":"去掉未经环境确认的操作建议"}
```

```json
{"decision":"escalate","expected_version":1,"comment":"需要人工核实环境与权限"}
```

edit 保留原动作，必须同时提供文本和理由；escalate 必须有理由。一条运行仅接受一份不可变审核。proposal/final_reply 保留 Agent 原文，人工修改在 review.edited_reply，实际消息由 published_message_id 关联。

| 事件 | 工单状态 |
| --- | --- |
| 生成待审核提案 | 保持 open，版本不变 |
| 批准解决建议 | 保持 open，追加回复，version +1 |
| 批准追问 | awaiting_customer，追加追问，version +1 |
| 客户补充 | awaiting_customer/open → open，version +1；用新消息、新 key 新建运行 |
| 审核转人工 | escalated，保存转人工记录，version +1，不再自动触发 Agent |
| 人工回复 | 状态不变，version +1，旧待审方案失效 |
| 明确确认解决 | resolved，记录关闭人、原因、时间，version +1 |

escalated 可接收客户补充、人工回复，仍留在人工队列，由 reviewer 关闭。resolved 是终态，新问题另建工单。新消息/关闭会使 waiting_review、failed 且有未应用审核的旧方案 cancelled；running 期间消息、关闭、新运行均返回 409。

## 审核与恢复

先以短事务保存 running 和快照，在事务外计算，再保存 waiting_review。外层持久化图为 compute → review(interrupt) → END；compute 先把持久化 snapshot 投影为最小 `AgentRunInput`，再执行检索图和受限决策循环。检查点只保存可序列化数据，不放 ORM Session 或客户端。

审核顺序：短事务保存不可变审核并领取 running → 事务外从该记录构造 Command(resume=...) → 短事务同时追加消息、更新工单、标记审核 applied 和运行 completed。interrupt 节点没有模型调用或业务写入。

| 状态 | 恢复行为 |
| --- | --- |
| waiting_review | 重启保留，同一 run/thread 继续审核 |
| 遗留 running，无审核 | 启动标记 failed / execution_interrupted；确认预算后用新 key 新建运行 |
| 遗留 running，有未应用审核 | 启动标记 failed / review_interrupted；重试原审核 |
| failed，审核应用失败 | 审核保留，重试原 key 和完整请求，不更换审核内容 |
| 图结束但业务未提交 | 核对检查点与审核一致，仅补业务事务，不盲目再次 resume |
| completed | 重复审核返回原结果，不再追加消息 |

未应用的失败审核阻止创建新运行；可以重试原审核，或追加新信息使旧方案失效。检查点缺失/不在 review 中断处时明确失败，不自动重算。旧 M1 待审记录没有检查点，不能冒充 M2 可恢复记录；应补充信息使其失效后，在授权预算内重新处理。

计算中断不承诺从每个工具调用继续。数据库持续不可写可能使失败状态暂时无法落库，重启扫描再识别遗留 running。90 秒仍是协作式超时，不能硬终止 SDK 卡死或持续流式响应。

## 分层验证与预算

```powershell
# 默认逻辑/替身测试，跳过真实 PostgreSQL
uv run --no-sync python -m pytest -q
uv run --no-sync python scripts/check_corpus.py
uv run --no-sync python scripts/check_ticket_graph.py --check-only

# 真实 PostgreSQL + checkpointer + ASGI HTTP，Agent 是合成替身
$env:TICKETMIND_RUN_DB_TESTS = '1'
uv run --no-sync python -m pytest -q
Remove-Item Env:TICKETMIND_RUN_DB_TESTS

# 独立进程实际终止/重启 + loopback HTTP + PostgreSQL，Agent 是合成替身
uv run --no-sync python scripts/check_m2_recovery.py
```

真实数据库测试仅写随机 tm_test_&lt;uuid&gt; schema，迁移后只清理该 schema，账号需 CREATE SCHEMA 权限。可用 TICKETMIND_TEST_DATABASE_URL 指向独立测试库。进程验收只启动并终止脚本自己的隐藏测试进程，报告位于被忽略的 data/cache/m2/process_recovery.json。

离线测试仍需配置数据库地址供模块导入；可临时使用 postgresql+psycopg://unused:unused@127.0.0.1:5432/ticketmind_test，仅用于默认 pytest，不用于迁移或真实测试。

M0 真实缓存回放并只读查询 Milvus：

```powershell
uv run --no-sync python scripts/check_ticket_graph.py
uv run --no-sync python scripts/check_ticket_flow.py --check-only
```

不带 --allow-* 时，缓存缺失/损坏/指纹不匹配均失败，禁止补调模型。M0 固定样本是“API 调用失败 / 今天上午调用订单查询接口时多次返回 E_TIMEOUT。运行环境是 Python 3.12，昨天还可以正常调用。”当前图缓存只保留查询向量缓存；旧 `understanding` 缓存属于历史证据，不再参与当前 Agent 运行。不能修改指纹或混用旧 CSV 样本。

M2 提示词和工具协议变更后，原 decision_m1.json 不再匹配，保留为历史证据。check_ticket_flow.py 默认使用独立 decision_m2.json，报告使用 m2_flow_verification.json；只验证到待审，--allow-decision 仍限明确授权的一次决策尝试。若模型请求新查询或下一次决策，缺少匹配缓存/额度便失败，不能视为完整多轮验收。不要覆盖旧 M1 缓存。

**M0/M1 一次性授权均已用完。M2累计21次请求尝试：理解3、首次Embedding3、重检索Embedding1、决策14（含1次模型名称NotFound失败）。20次有返回，失败请求无usage，不将尝试数等同于收费账单或业务成功数。** 最新GLM验收含1次失败名称请求、2次有效决策、1次重检索向量；审核缓存回放新增0。失败样本保留，不自动重试或挪用预算；新输入、提示词和额外实验需另行核对授权范围。旧 check_qwen.py、check_embeddings.py 及缺缓存的 check_dense_retrieval.py 会调用模型，不是无付费预检；`check_understanding.py` 已随 understanding 阶段删除。

追问新增中英文操作建议负例约束；解决提案要求实际证据；输入风险规则独立于模型自报。这些是保守启发式，可能误拒绝或漏检，不能证明所有建议安全且适用于客户环境，仍须人工审核。三例真实输出与逐项质量核对见 [审核单](docs/m2-acceptance-review.md)，不作为准确率评测。

### M2 三路径验收入口

[可审阅方案与预算](docs/m2-acceptance.md)、[完整合成输入](docs/m2-acceptance-cases.json)已准备好。默认仅离线预检：

```powershell
uv run --no-sync python scripts/check_m2_acceptance.py
```

新增额度默认全部为 0。三例预算上限：理解 2、首次向量 2、重检索向量 3、决策 9（首次 3、后续 6），合计最多 16 次；若全部直接决策结束则最多 7 次。参数不代表授权。真实客户补充后的新模型运行另算，未包含在此额度内。

新入口用 ASGI HTTP API、临时 PostgreSQL/checkpointer、真实只读 Milvus，按完整指纹缓存和累计台账限制模型请求。每次发送前记账，失败也计次；原始决策在结构/规则校验前保存。默认停在待审核，人工看过原文后才能提供绑定 proposal_hash 的审核文件，再以零新增额度回放并验证业务应用。详见方案中的执行命令。

模型原始动作、代码最终提案、人工修改和实际发布分别报告；不自动宣告提案质量通过。模型未自然选择重检索时，保留该项未验证。输出在被忽略的 data/cache/m2/acceptance/，不得删除台账重置预算。进程异常留下 session.lock 时先确认没有该验收进程；不自动解除锁或清空证据。

Windows 环境若 pytest 默认临时目录不可写，可指定仓库内**每次新建**的专用临时目录，不改现有目录权限：

```powershell
$m2Temp = Join-Path (Get-Location) ('data/cache/m2/pytest-' + [guid]::NewGuid().ToString('N'))
uv run --no-sync python -m pytest -q -p no:cacheprovider --basetemp $m2Temp
```

新验收测试为 tests/test_m2_acceptance.py（逻辑/模型替身）和 tests/integration/test_m2_acceptance_http.py（真实 PostgreSQL，模型与 Milvus 替身）；真实数据库测试仍需 TICKETMIND_RUN_DB_TESTS=1。

剩余“客户补充后继续处理＋真实重检索”合并方案见 [M2 补充验收](docs/m2-followup.md)。新入口 `scripts/check_m2_followup.py --prepare` 只复用旧缓存、写临时客户补充并核对新快照指纹；实际新模型阶段需要该场景独立授权，当前 runtime 不再包含 understanding 调用。首次漏掉006是明确记录的验收注入，不是对自然首检效果的测量。

## Milvus 与历史案例

```powershell
docker compose --project-directory infra/milvus up -d
docker compose --project-directory infra/milvus ps -a
uv run --no-sync python scripts/check_milvus.py
```

先启动 Docker Desktop 的 Linux Engine。infra/milvus/.env 的 DOCKER_VOLUME_DIRECTORY 控制数据根目录；保留现有 volumes，日常停止用 docker compose --project-directory infra/milvus stop。

以下是固定 M0—M4 评测兼容入口，正式知识初始化使用本文开头的 `knowledge.py`。旧 Dense 集合绑定原始语料内容版本。兼容导入脚本现在也先 seed PostgreSQL、从 PG 取正文，再复用 data/cache/embeddings/ 中匹配的向量；`--versioned` 通过知识同步服务更新状态。整批 `--allow-embedding` 仍须另行授权；生产新增向量使用带累计台账的新命令。保留 historical_cases_v1 与原 M3 集合。

### M3 初始化、切换与验收

```powershell
# 创建内容版本化新集合，然后复用文档向量并核对 ID、文本、版本和条数
uv run --no-sync python scripts/init_case_collection.py --versioned
uv run --no-sync python scripts/ingest_historical_cases.py --versioned

# 仅影响当前终端启动的 API；持久配置可写入本地 .env，修改后重启 API
$env:TICKETMIND_RETRIEVAL_MODE = 'hybrid'
$env:TICKETMIND_AGENT_VERSION = 'ticketmind-m3'
$env:TICKETMIND_KNOWLEDGE_DATASET = 'synthetic-v2-e5b5a59a7e1481ad3b095d518772354155d891e51ad2a367cf5f7be26540228f'
uv run --no-sync uvicorn ticketmind.main:app --host 127.0.0.1 --port 8000 --workers 1
```

固定评测适配器切回 dense 仍使用原 V2 旧 Dense 基线。正式 API 的三种模式均使用 `TICKETMIND_KNOWLEDGE_DATASET` 对应的 PG 登记集合；即使设为合成数据集，也从 PG 回填正文。bm25 不调用查询 Embedding。版本不匹配会明确失败，不向旧集合或 JSONL 补结果；API 不接收任意集合名。

```powershell
# 固定 5 条历史查询；精确缓存缺失即失败，不补调模型
uv run --no-sync python scripts/evaluate_retrieval.py --check-only
uv run --no-sync python scripts/evaluate_retrieval.py
# 新增 12 条诊断样本可先只跑无需向量的真实 BM25
uv run --no-sync python scripts/evaluate_retrieval.py --queries data/synthetic/m3/retrieval_diagnostics.jsonl --modes bm25
# 真实 PostgreSQL/checkpointer + ASGI HTTP + Milvus；理解/决策为明确标记的替身
uv run --no-sync python scripts/check_m3_flow.py
```

报告默认保存为 data/cache/m3/ 下带时间戳的新文件。各通道候选、原始分数、排名、耗时和错误写入运行 tool_calls；最终证据保存 dense_score / bm25_score / fusion_score，不把分数混成置信度。Hybrid 任一路空结果或失败使运行 failed，已取得的候选仍保留在诊断中。

M3 使用 m3-retrieval-evidence-v1 决策输入协议；旧决策缓存保留，但不能冒充当前证据结构的匹配缓存。M0 理解/向量缓存仍可复用。历史 M2 缓存审核命令属于历史协议证据；本轮未申请、执行任何新付费模型调用，也未重验真实模型对 Hybrid 证据的决策质量。

实际证据见 [开发日志](docs/project-log.md)。恢复设计参考 [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 和 [PostgreSQL persistence](https://docs.langchain.com/oss/python/langgraph/add-memory)，以锁定依赖和本仓库测试为准。

## M4 评测入口

```powershell
# 离线校验输入、候选标签与精确缓存；不调用任何模型
uv run --no-sync python scripts/evaluate_m4.py
# 真实只读检索；缺向量的 Dense/Hybrid 单独记录 not_run
uv run --no-sync python scripts/evaluate_m4.py --stage retrieval --include-m3-diagnostics
```

新数据在 `data/synthetic/m4/`，validation/test按问题组隔离；v2和M3仍是开发数据。`human_review_required`（发布需审核）、`requires_human_handoff`（业务须转人工）、相关来源和可作答性分开标注。候选文件保持pending_review；用户委托完成的72条审核保存于独立`label_reviews.jsonl`及`development_label_reviews.jsonl`，入口默认加载相邻审核文件并校验输入、标签和语料版本hash。审查方法明确记为user_delegated_agent。

`--stage vectors/agent --execute`可能发生付费调用，默认各项累计上限为0；先按[预算方案](docs/m4-call-budget.md)获得授权。台账固定在`data/cache/m4/attempts.json`，失败计次、不自动重试。Agent入口只保存待审结果；原始模型响应、代码最终提案和人工业务结果分别保留。评分器支持动作、引用、无依据建议和人工语义判断，详见[M4报告](docs/m4-evaluation.md)。
