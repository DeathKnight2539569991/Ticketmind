# TicketMind

面向合成 SaaS 技术支持工单的 Agent 项目。当前 M1 支持：身份认证 → 创建/查询工单 → 从数据库读取工单 → 理解 → 历史案例 Dense 检索 → 结构化决策 → 保存待审核提案。支持建议、追问、转人工三种提案；**审核接口、回复发布、关闭工单和可恢复人工审核尚未实现**，由 M2 补齐。

历史案例与评测输入来自 `data/synthetic/v2/`，均为合成场景；12 条历史案例和 16 条评测输入仅为开发集，不代表真实客户业务效果。

## 安装与配置

以下 PowerShell 命令从本仓库目录运行（本机为 `D:\AnalyzeAgent\app`），需要 Python 3.12 和 uv。依赖以 `uv.lock` 为准，不整体升级。

```powershell
Set-Location D:\AnalyzeAgent\app
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item env.example .env }
```

编辑 `.env` 填入自己的值；保留已有 `.env`，不要提交密钥或缓存。

| 配置 | 用途 |
| --- | --- |
| `TICKETMIND_DATABASE_URL` | 已创建的 PostgreSQL 数据库及有迁移权限的账号，使用 `postgresql+psycopg://...` |
| `DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE_ID` | 阿里云北京地域业务空间凭据；本地预检只校验存在，不验证凭据有效性 |
| `TICKETMIND_MODEL` | 默认 `qwen3.7-flash`，用于结构化理解 |
| `TICKETMIND_EMBEDDING_MODEL` | 当前集合固定 `text-embedding-v4`、1024 维 |
| `TICKETMIND_MILVUS_URI` | 本地默认 `http://127.0.0.1:19530` |
| `TICKETMIND_MILVUS_TIMEOUT_SECONDS` | Milvus 调用超时，默认 10 秒，不是图总时间预算 |
| `TICKETMIND_OPERATOR_TOKEN`、`TICKETMIND_REVIEWER_TOKEN` | 独立 Bearer 凭据，至少 32 个非空白 ASCII 字符；可用下方脚本生成 |
| `TICKETMIND_OPERATOR_ID`、`TICKETMIND_REVIEWER_ID` | 服务端预置 actor_id，默认 operator/reviewer，必须不同 |
| `TICKETMIND_AGENT_VERSION`、`TICKETMIND_RETRIEVAL_MODE`、`TICKETMIND_RETRIEVAL_TOP_K` | 默认 ticketmind-m1、dense、3；M1 仅接受 dense |
| `TICKETMIND_PROCESSING_TIMEOUT_SECONDS` | 默认 90 秒，阶段间检查剩余时间，外部请求使用剩余预算内的超时 |
| `TICKETMIND_CORPUS_PATH` | 可选语料路径；默认项目内 v2 历史案例，版本按内容计算 |
| `TICKETMIND_APP_NAME`、`TICKETMIND_ENVIRONMENT`、`TICKETMIND_LOG_LEVEL` | 应用基础设置；目前未据此配置完整日志系统 |

模型地址由业务空间拼接：`https://<workspace>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`（理解）和 `/api/v1`（Embedding）。当前应用的本地 Milvus 配置未实现鉴权；不要把本地服务直接暴露公网。

## 无外部调用检查

```powershell
uv run --no-sync python -m pytest -q
uv run --no-sync python scripts/check_corpus.py
uv run --no-sync python scripts/check_ticket_graph.py --check-only
```

默认 pytest 跳过需要显式开启的真实 PostgreSQL 集成测试，其余缓存/失败路径检查使用合成替身；API 健康检查不连接数据库。收集测试需要数据库配置，但不会证明 PostgreSQL 可用。`--check-only` 校验模型/Milvus 配置、缓存指纹和缓存目录写入权限，不创建外部客户端、不调用模型；缺失缓存会明确显示 `missing`，此模式通过不表示可以回放图。

只想运行离线测试且尚未配置数据库时，可为当前 PowerShell 进程设置测试用地址：

```powershell
$env:TICKETMIND_DATABASE_URL = 'postgresql+psycopg://unused:unused@127.0.0.1:5432/ticketmind_test'
uv run --no-sync python -m pytest -q
Remove-Item Env:TICKETMIND_DATABASE_URL
```

不要将这个地址用于真实迁移。以下步骤需要真实服务。

## PostgreSQL 与 API

先准备 PostgreSQL，创建 `.env` 指定的数据库和账号，再执行已有迁移；本仓库目前没有统一 PostgreSQL Compose 配置。

```powershell
uv run --no-sync python scripts/configure_local_auth.py
uv run --no-sync alembic upgrade head
uv run --no-sync alembic current
uv run --no-sync uvicorn ticketmind.main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 [API 文档](http://127.0.0.1:8000/docs) 或访问 [健康检查](http://127.0.0.1:8000/health)。除健康检查外的业务接口需要 Bearer 身份；`requester_role` 只是描述字段，不参与授权。身份脚本只填充 `.env` 中缺失的凭据，不打印或替换已有值。每个预置凭据对应一个操作者，不应多人共享。创建工单会写数据库，健康检查只说明 API 进程存活。

M1 新迁移为 `6b31a12c9e01`，在原三表上增加版本、幂等、运行快照与审计字段；原有行保留，旧记录新增审计字段允许 null。若旧库有同工单多条 running，新增唯一索引会明确失败，需要先检查这些旧记录，不自动删改。M1 回退遇 waiting_review 会拒绝，避免强行篡改状态。

## M1 API 使用流程

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/tickets` | 创建工单和首条消息；要求 Idempotency-Key |
| GET | `/tickets` | status、limit、offset 过滤/分页，稳定倒序 |
| GET | `/tickets/{ticket_id}` | 版本、有序消息、最新运行 |
| POST | `/tickets/{ticket_id}/runs` | trigger_message_id、expected_version；要求 Idempotency-Key |
| GET | `/tickets/{ticket_id}/runs` | 历史运行分页 |
| GET | `/tickets/{ticket_id}/runs/{run_id}` | 提案、理解、证据、模型版本、usage 或失败信息 |
| GET | `/sources/{source_id}?corpus_version=...` | 指定语料版本的完整合成案例 |

在 Swagger 的 Authorize 中输入本地 Bearer 凭据，或按以下 PowerShell 示例调用。此示例假定凭据由上方脚本生成；它只读取到变量，不打印凭据。新环境不要覆盖已有 `.env`。

```powershell
$tokenLine = Get-Content .env | Where-Object { $_ -match '^TICKETMIND_OPERATOR_TOKEN=' }
$operatorToken = ($tokenLine -split '=', 2)[1]
$headers = @{ Authorization = "Bearer $operatorToken"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$payload = @{ subject = 'API 调用失败'; body = '今天上午调用订单查询接口时多次返回 E_TIMEOUT。运行环境是 Python 3.12，昨天还可以正常调用。'; channel = 'api'; requester_role = 'customer' } | ConvertTo-Json
$ticket = Invoke-RestMethod http://127.0.0.1:8000/tickets -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($payload))
$detail = Invoke-RestMethod "http://127.0.0.1:8000/tickets/$($ticket.id)" -Headers $headers
$headers['Idempotency-Key'] = [guid]::NewGuid().ToString()
$runPayload = @{ trigger_message_id = $detail.messages[-1].id; expected_version = $detail.version } | ConvertTo-Json
# 以下 POST 会调用真实理解、Embedding 和决策模型；仅在已授权预算内执行
$run = Invoke-RestMethod "http://127.0.0.1:8000/tickets/$($ticket.id)/runs" -Method Post -Headers $headers -ContentType 'application/json' -Body $runPayload
$run | Select-Object id, run_status, action, final_reply, error_code, error_summary
```

保存本次 Idempotency-Key 和请求体，网络中断时使用相同内容重试。相同操作者/资源/key 和相同请求返回原记录（200），内容不同返回 409；重新执行失败运行要使用新 key。首次创建运行的 201 不等于模型成功，必须检查 `run_status`。

处理先提交 running 和工单快照，在事务外执行模型/Milvus，再保存 waiting_review 或 failed。相同工单的 running/waiting_review 由行锁和数据库部分唯一索引保护；等待审核时不能重复创建新运行。`resolve` 是建议动作，不会发布回复或将工单置为 resolved；三类提案均先等待审核。

当前来源接口只提供已配置语料的内容版本；若旧版本不可用会明确返回 404，历史运行的证据快照仍保留，不静默换成新案例。候选文本与本地语料不一致会使运行失败。模型引用只允许来自本次实际检索结果，COSINE 分数不作为概率或自动解决阈值。

## M1 分层验证

真实 PostgreSQL 集成测试（Agent 使用合成替身，不调用模型；其中一项使用自有本地未监听端口验证真实连接失败落库）：

```powershell
$env:TICKETMIND_RUN_DB_TESTS = '1'
uv run --no-sync python -m pytest -q
Remove-Item Env:TICKETMIND_RUN_DB_TESTS
```

测试默认使用已配置数据库中的随机 `tm_test_<uuid>` schema，执行真实迁移后仅清理该 schema，不触碰 public 业务表。账号需要 CREATE SCHEMA 权限。可设置 `TICKETMIND_TEST_DATABASE_URL` 将 pytest 指向独立测试数据库。

真实 HTTP（临时 loopback Uvicorn）、PostgreSQL、Milvus 验收，复用匹配的 M0 理解/向量缓存：

```powershell
uv run --no-sync python scripts/check_ticket_flow.py --check-only
# 已有匹配的真实决策缓存时，以下命令没有新模型调用
uv run --no-sync python scripts/check_ticket_flow.py
# 缺少决策缓存且明确授权一次调用时才加 --allow-decision
```

该脚本也只写随机测试 schema，退出后清理；正式 API 不启用开发缓存。验收检查创建工单、创建运行、查询与数据库一致、幂等 200、冲突 409、来源查询及工单仍 open。决策缓存及验收报告保存在 `data/cache/graph/api_timeout/`，不提交 Git。决策缓存指纹包含完整工单、理解、实际证据、模型和提示词；不匹配就失败，不会自动重调。

M1 仍有边界：90 秒预算按阶段检查并传给外部 SDK，属于协作式超时，不保证对持续流式字节/进程卡死的硬中断；进程退出或数据库持续不可写时，running 可能遗留，启动恢复在 M2 实现。没有审核接口、interrupt/checkpointer 或客户补充接口。真实验收草稿曾包含未经环境确认的“停用本地代理”建议，说明结构和引用有效仍不足以证明业务建议安全；回复必须经人工审核，不能直接发布。

## Milvus 与历史案例

先启动 Docker Desktop 并等待 Linux Engine 就绪：

```powershell
docker compose --project-directory infra/milvus up -d
docker compose --project-directory infra/milvus ps -a
uv run --no-sync python scripts/check_milvus.py
```

`infra/milvus/.env` 可配置 `DOCKER_VOLUME_DIRECTORY`（Compose 数据根目录，不是应用配置）；未指定时使用该 Compose 目录。保留现有 `volumes`，日常停止使用 `docker compose --project-directory infra/milvus stop`。

首次建集合和导入（这些命令会写本地 Milvus）：

```powershell
uv run --no-sync python scripts/init_case_collection.py
uv run --no-sync python scripts/ingest_historical_cases.py
```

导入优先复用 `data/cache/embeddings/` 中匹配的文档向量；缓存缺失时默认失败。只有另行授权生成整批历史案例向量后，才使用 `ingest_historical_cases.py --allow-embedding`。**图验证的一次查询向量授权不包含整批入库调用。** 现有导入仍按 12 条开发语料检查；M0 不迁移或清空集合。

## 验证理解 → 检索

入口为 `scripts/check_ticket_graph.py`，沿用脚本中的固定合成样本：

> 标题：API 调用失败
>
> 正文：今天上午调用订单查询接口时多次返回 E_TIMEOUT。运行环境是 Python 3.12，昨天还可以正常调用。

默认只复用真实模型缓存，重新查询真实 Milvus：

```powershell
uv run --no-sync python scripts/check_ticket_graph.py
```

如果缓存缺失，先取得对上述样本发送至已配置 DashScope 北京业务空间的明确授权，再执行：

```powershell
uv run --no-sync python scripts/check_ticket_graph.py --allow-understanding --allow-embedding
```

脚本先检查两份缓存、写入权限以及真实 Milvus 集合存在、维度、COSINE 索引和非空记录，再执行图。理解成功后立即原子保存，查询向量成功后立即原子保存，随后检索；Milvus 搜索失败不会丢掉成功的模型结果。OpenAI 客户端在上下文退出时关闭，Milvus 由 `finally` 关闭；开发 Embedding 请求使用有单次 HTTP 发送上限的 Session 并关闭它，以阻止 SDK 内部连接重试/重定向产生第二次请求。

两份缓存位于 `data/cache/graph/api_timeout/{understanding,query}.json`。指纹包含实际输入、业务空间端点、模型；理解还包含提示词、结构定义及生成配置，查询包含维度和 `text_type=query`。缓存损坏、输入/配置/元数据不匹配会明确失败，不能偷偷重调模型或套用旧向量。更换样本或配置时使用独立 `--cache-dir`；保留旧缓存，不手改指纹。旧 `csv_separator.json` 的查询文本不同，不能用于本图。

成功后再运行不带授权开关的命令，应看到两个 `cache_hits=1`、两个 `attempts=0`，同时有本次 Milvus 返回的证据。计数是本次运行尝试数，不是服务端计费或 token usage；允许开关不形成跨进程预算管理。失败后根据已保存的缓存只批准仍缺失的一项，不能反复使用开关消耗新的请求。

业务图本身不读固定缓存或默认拒绝新工单：`build_ticket_graph` 接收 settings、embeddings、client、top_k、timeout；开发脚本通过 `understanding_fn` 和 Embedding 适配器注入缓存。图输入是标题/正文，输出保留原文并增加结构化理解、查询文本和证据；检索仍使用原始标题与正文。

旧的 `check_qwen.py`、`check_understanding.py`、`check_embeddings.py` 会直接调用模型，旧 `check_dense_retrieval.py` 缺缓存时也会调用模型；不要把它们当作无付费预检命令。

## 当前验证边界

实际结果与剩余范围见 [开发日志](docs/project-log.md)。静态/逻辑检查、模型缓存回放、真实模型加 Milvus、真实 PostgreSQL/API 落库分别记录。M0 图脚本只证明理解与检索；M1 流程脚本验证到待审核结果落库，尚未覆盖人工审核、客户补充和关闭工单的完整业务闭环。
