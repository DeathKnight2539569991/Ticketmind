# 启动与 3—5 分钟演示

## 零付费隔离演示

前提：Python 3.12、uv、可连接且可创建 schema 的 PostgreSQL。复制根目录 `env.example` 为 `.env`，配置数据库；无需有效模型凭据和 Milvus。

```powershell
uv sync --locked
uv run --no-sync python scripts/start_local.py --demo --check
uv run --no-sync python scripts/start_local.py --demo
```

打开 `http://127.0.0.1:8501`。终端打印本次 `data/cache/m5/demo-tm_test_<随机值>.json` 文件路径，从该本地文件取 reviewer_token 登录。凭据仅用于本次隔离演示，不要截图或提交。页面顶部黄色提示明确说明替身模式。API 默认端口 8010，工作台 8501。

每次演示新建独立 schema 并运行真实迁移和 checkpointer。正常 Ctrl+C 退出会清理本次 schema 与凭据文件，不接触原业务表；强制杀进程/断电可能留下 `tm_test_...` schema，须核对当次凭据文件记录再单独处理。脚本不会扫描并删除历史 schema。

运行状态、审核、消息和关闭真实落库；Agent 路由与检索命中来自明确的确定性替身。这是 UI/业务流程演示，不能作为真实模型效果的证明。

## 演示脚本（约 4 分钟）

| 时间 | 操作与讲解 |
| --- | --- |
| 0:00—0:35 | 展示合成演示标记和 reviewer 身份。创建标题“正常建议”的工单，正文写“本机代理连接失败”，确认创建。 |
| 0:35—1:25 | 工单操作 → 处理最新客户消息 → 确认。切到运行与审核，展示待审草稿、来源 SYN-HIST-V2-007 和调用记录。编辑后批准，说明修改理由。展示原始提案与实际发布消息不同，工单仍 open；明确确认解决后才关闭。 |
| 1:25—2:25 | 创建标题“补问继续”，处理并批准补问，工单变等待客户。录入客户补充“已启用本机 HTTP 代理”，再次处理并审核新建议。展示两个运行与有序消息。 |
| 2:25—3:15 | 创建标题“转人工”，处理并批准转人工。工单显示已转人工，Agent 处理按钮禁用；录入人工回复并确认解决。 |
| 3:15—3:45 | 创建标题“故障演示”，处理后显示 failed 和稳定错误码，客户原文仍在。不要将 HTTP 201 当作成功。 |
| 3:45—4:10 | 展示 M4 报告：6 条真实 Agent 动作匹配 5/6，3 条草稿错误声称已转交；工作台支持人工检查，尚未证明模型问题已修复。 |

演示标题是替身的公开路由规则：含“故障”→失败，含“转人工”→转人工，含“补问”且未批准过追问→补问，其余→建议。仅隔离演示使用这些规则。

本轮提供脚本与可重复演示环境，未录制成片。真实模型演示应使用正式模式，先获得具体调用授权，按实际结果调整讲解，不能用替身录像冒充真实模型。

## 正式模式：现有 PostgreSQL / Milvus

```powershell
uv sync --locked
uv run --no-sync python scripts/configure_local_auth.py
# 启动 Docker Desktop Linux Engine；数据库使用 .env 中现有连接
uv run --no-sync python scripts/start_local.py
# 依赖已经运行时：
# uv run --no-sync python scripts/start_local.py --skip-infra
# Windows PostgreSQL 服务未启动时，可明确指定本机实际服务名：
# uv run --no-sync python scripts/start_local.py --postgres-service postgresql-x64-18
```

启动器检查端口、启动现有 Milvus Compose、验证 PostgreSQL、执行 Alembic 升级，然后启动单 worker API 和 UI。Ctrl+C 停止自己启动的 API/UI，保留数据库、Milvus 与数据目录。`--check` 只验证 DB 与 API/UI 端口；不表示 Milvus、凭据或模型可用。根目录 `.env` 不被覆盖。

启动器不自动导入语料、生成向量或运行模型。第一次初始化请按 README 的 Milvus 章节执行 `init_case_collection.py` 与 `ingest_historical_cases.py`。Hybrid/BM25 使用 `--versioned`；Dense 默认基线需旧集合。新机器没有忽略提交的向量缓存，导入需要自行配置真实服务，并在明确授权后添加 `--allow-embedding`，12 条历史案例缺失向量时最多生成 12 个文档向量请求。已有匹配向量可复用。每次正式“处理”也可能付费。

正式模式从根目录 `.env` 取分配给自己的 operator/reviewer 凭据登录，UI 不自动选择服务端高权限凭据。API URL 可用环境变量覆盖，默认 `http://127.0.0.1:8000`；远端 API 仅允许 HTTPS，部署时另行解决 TLS 与服务访问控制。

## 新机器可选 PostgreSQL 容器

```powershell
Copy-Item infra/postgres/env.example infra/postgres/.env
# 编辑该文件，设置独立 POSTGRES_PASSWORD
# 编辑根 .env：TICKETMIND_DATABASE_URL=postgresql+psycopg://ticketmind_app:<URL编码后的密码>@127.0.0.1:5433/ticketmind_dev
uv run --no-sync python scripts/start_local.py --postgres-container --demo
```

已有 PostgreSQL 无需迁移。容器只绑定本机 5433，数据使用命名 volume `ticketmind_pg`，正常停止用 `docker compose --project-directory infra/postgres stop`，不执行 `down -v`。这是可选新环境路径，本轮是否实测见 M5 验收报告。
