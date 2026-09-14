# TicketMind 项目开发日志

> 更新：2026-09-14。维护当前实现、关键决策、验证证据和待办，不再记录逐轮教学。
> 仓库：`D:\AnalyzeAgent\app`。沿用最新源码；用户已授权将 M0/M1 成果提交并推送 GitHub，未部署。协作规范见上级 AGENTS.md。

## 当前状态

- M0 已完成：真实理解 → Dense 检索，开发缓存与业务图分离。
- M1 已实现并验证到待审核提案落库：Bearer 身份 → HTTP 工单/运行 → 数据库快照 → 理解/检索/决策 → waiting_review 或 failed。
- 三类提案为 propose_resolution / ask_clarification / escalate，映射已有 AgentAction；不发布客户消息，不自动关闭工单。
- 尚未实现 M2 人工审核、消息追加、关闭、interrupt/checkpointer、进程中断恢复；M3 才引入 BM25/Hybrid。
- 运行边界：单实例、单 worker、一个团队、两个预置独立身份；不宣称多租户或可靠后台队列。

## 关键实现与约束

| 模块 | 职责 / 决策 |
| --- | --- |
| core/auth.py、config.py | 独立 operator/reviewer Bearer 凭据映射可信 actor_id；requester_role 不参与授权 |
| api/routes/{tickets,runs,sources}.py | 创建/分页/详情、运行创建/查询、版本化来源；写入要求 Idempotency-Key |
| tickets/processing.py | 短事务保存 running 和输入快照，事务外调用 Agent，短事务保存提案/失败；相同请求先查幂等再查版本 |
| tickets/models.py / 迁移 | Ticket.version、操作者/请求摘要、运行快照/版本/耗时/usage；行锁及部分唯一索引限制同工单一个 active run |
| agent/graph.py、runtime.py | 理解 → Dense 检索 → 决策；M0 入口可不接决策；客户端由运行边界关闭 |
| agent/proposals.py、decide.py | 区分三类结构，验证必要问题、实际来源引用和高风险标记；结构化 JSON，不宣称原生 Tool Calling |
| knowledge/sources.py | 对完整历史案例计算内容版本；Milvus 命中文本必须与本次语料一致；旧版本不可用时明确 404 |
| agent/dev_cache.py、dev_decision_cache.py | 仅验收脚本启用真实结果缓存，按输入/模型/配置/证据校验；成功立即原子保存，默认禁止新调用 |
| retrieval/transport.py | 单次 HTTP send 保护，拦截锁定 DashScope SDK 的连接重发；不升级依赖 |

- 同 key 同请求返回原记录（200），不同内容返回 409；首次运行的 201 只表示记录建立，必须检查 run_status。
- trigger_message_id 必须属于本工单且为最新消息，正文与有序消息由数据库读取；客户端不能传入正文、actor_id 或 thread_id。
- 提案、证据快照、来源版本、真实模型配置、可取得的 usage 持久化；confidence 保持 null，不把 COSINE 当概率。
- 所有三类提案都进入 waiting_review，Ticket 仍 open；审核前不追加客户可见回复。
- 模型/检索失败记录阶段和稳定错误码，返回信息不含原始异常/凭据；服务端日志仅记录 run_id/request_id、阶段和异常类型。
- 当前预算为阶段间检查与 SDK 超时，非进程级硬截止。数据库持续故障或进程退出仍可能留下 running；恢复机制在 M2。
- 正式 API 不复用开发缓存；旧 CSV 查询向量与图输入不同，始终保留但不混用。

## 验证证据

### M0（2026-09-14）

- 锁文件离线同步通过；当时 32 项逻辑测试通过。
- 真实理解和 Embedding 各一次，Milvus 返回 SYN-HIST-V2-007 / 006 / 008，COSINE 约 0.6227 / 0.5854 / 0.5673。
- 第二次理解/Embedding 调用均为 0，两份真实缓存各命中一次，重新查询真实 Milvus 结果一致。
- 最初 Docker/Milvus 不可用和具体模型授权不足的阻塞，均已在用户准备环境并确认后解除。

### M1（2026-09-14）

- `uv sync --locked` 通过，锁文件及依赖版本未变。首次离线同步缺少 uv-build 构建缓存，联网取得构建依赖后只重建本项目包。
- `TICKETMIND_RUN_DB_TESTS=1 python -m pytest -q`：**58 passed**；45 项逻辑/合成替身测试、13 项真实 PostgreSQL 集成测试。2 条依赖弃用警告仍保留，未为消除警告升级。
- 集成测试包括：真实迁移/旧数据保留、HTTP/数据库一致、三类提案、版本/归属校验、幂等、同工单并发与数据库唯一约束、失败保存、事务外网络边界、真实自有未监听端口连接失败。模型业务输出使用测试替身，单独标注。
- 真实验收：`python scripts/check_ticket_flow.py --allow-decision` 退出码 0。使用实际 loopback HTTP、独立临时 PostgreSQL schema 和真实 Milvus；理解/向量复用 M0 缓存，只新增 **1 次决策调用**。
- 验收结果：action=ask_clarification，run_status=waiting_review，工单=open；同请求重发 200、同 key 改内容 409；HTTP 提案/证据与数据库一致，来源查询成功。
- 决策 usage：prompt_tokens=2039、completion_tokens=476、total_tokens=2515；模型/检索处理耗时 7500 ms（单例，不是性能基准）。本轮新增理解/Embedding 调用均为 0，不混同全新模型链路。
- 原始验收报告和决策缓存保存在被忽略的 `data/cache/graph/api_timeout/{m1_verification,decision_m1}.json`；临时验收 schema 已清理，报告中的 UUID 不指向持久业务演示记录。
- 开发库已由 55ee2375ea43 升级至 **6b31a12c9e01**；迁移前后原业务行数均为工单 1 / 消息 1 / 处理结果 0。没有重建或删除现有业务表/集合。
- `.env` 已补缺失的两份随机本地身份凭据；未输出或提交凭据，既有数据库/模型配置保留。
- 使用本机实际配置验证：未认证访问工单列表 401，operator 凭据访问 200，读取原有 1 张工单。临时测试 schema 剩余 0。

### 已知质量问题与未验证项

- 真实草稿虽正确选择追问，但问题中附带“尝试停用本地代理”的操作建议，尚未确认环境。仅证明链路和结构有效，不能宣称回复可直接发布；M2 审核前应收紧追问规则并补负例验证。
- 仅一条合成样本使用真实决策；建议/转人工分支有逻辑及数据库验证，没有新增真实模型演示，也未形成准确率指标。
- 未验证全新理解 + Embedding + 决策的 M1 整链路；本次前两步为真实缓存复用。
- 未实现/验证人工审核、发布、关闭、多轮恢复或崩溃恢复；不把 waiting_review 业务状态等同 LangGraph interrupt。
- 锁定 Alembic 对 SQLAlchemy 非原生枚举的检查约束会产生“移除”误报；测试单独核验枚举允许值，其余字段/索引/约束差异仍检查。

## 运行入口与下一步

- 安装/配置/迁移/API 调用示例统一见 README.md，不在日志重复命令教程。
- `scripts/configure_local_auth.py` 只补缺失本地凭据；`scripts/check_ticket_graph.py` 验证 M0；`scripts/check_ticket_flow.py` 验证 M1。
- 真实模型额度：M0 的一次理解/Embedding 和 M1 的一次决策授权均已使用。后续先复用匹配缓存；新样本/新提示词产生的新调用需另行授权。
- 下一批 M2：受限重检索/决策循环、审核表与审核 API、消息追加和关闭、持久化 interrupt/resume、版本失效与审核幂等、启动中断恢复。
- 开始 M2 前明确新增真实模型验证预算；保持现有业务数据和缓存，不把未完成项计入简历能力。
