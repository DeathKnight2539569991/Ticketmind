# Tool / Docs 重构交付与验证（2026-10-06）

本次交付把 Agent 的检索入口改为 Cases / Docs 两种只读工具，并增加版本化 synthetic Markdown 产品文档。生产和直接 `AgentRunner` 调用共用一张图；人工审核、工单状态、检查点恢复、幂等发布与既有运行取消行为保持业务层负责。

本轮新增付费模型 / Embedding 调用 **0 次**。只实连本机 PostgreSQL，在 UUID 测试 schema 中迁移与写入；检索与模型采用确定性替身，真实 SDK 故障验收请求仅发送到测试自有 loopback endpoint。未实连真实 Milvus 或 LangSmith 云端，未进行真实模型决策验收，未提交、推送或生产部署。

本报告描述 `ticketmind-tools-docs-v2` / state version 2 的最新实现；[先前 durable 重构报告](agent-durable-refactor.md)保留原阶段历史。执行分工和阶段证据见[本次计划](tool-docs-refactor-plan.md)。

## 使用流程与执行契约

1. 创建工单后，`bootstrap_retrieve` 使用客户明确提供的标题和消息检索 Cases。
2. Decision 读取当前工单、实际 Cases / Docs 正文和工具拒绝记录，可选择五种动作：`search_cases`、`search_docs`、`propose_resolution`、`ask_clarification`、`escalate`。
3. 两种工具成功或被业务 guard 拒绝后回到 Decision；未知动作或非法参数由 schema 拒绝，基础设施/协议错误生成失败结果。
4. 最终候选经 Semantic Judge。第一次 FAIL 且有 step 时仅允许一次 final-only Repair，然后再 Judge；第二次 FAIL 仍失败。
5. 提案持久化为 `waiting_review`。reviewer 核对正文、来源、工具和 synthetic 标识，批准/编辑/转人工后业务层原子保存结果。发起处理与恢复均不发布客户回复、不关闭工单。

当前图为 `bootstrap_retrieve → decision ↔ search_cases/search_docs → judge → repair → judge → review`，包含预算分支与 `resume_gate`。standalone 使用同一拓扑，在已审查提案或固定预算转人工处 END；业务入口持久化 review interrupt。当前工具 schema 和 Judge capabilities 均只暴露 `search_cases` / `search_docs`；`get_case_detail` 仅保留在旧 checkpoint 识别逻辑，底层旧 repository 读取方法继续供其他合法路径使用。

| 计数 | 默认限制 | 实际规则 |
| --- | --- | --- |
| Case 成功搜索 | 2 | bootstrap 已占 1；后续成功才增加 |
| Docs 成功搜索 | 2 | 独立计数，可配置为 0 |
| agent_steps | 8 | bootstrap=1；Decision 模型调用、工具尝试（含 guard 拒绝）、实际 Repair 模型调用各占 1 |
| Repair | 最多 1 | 只生成最终提案，不能再次规划工具 |
| 主动澄清 | 最多 2 轮 | 达到限制后不得由 Repair 绕过 |
| active time | 90 秒 | 原累计计算耗时生效；每次模型调用超时上限为剩余预算与 30 秒的较小值 |

query 按 `strip().casefold()` 在各工具内去重，同一文本可以分别搜索 Cases / Docs。重复、额度耗尽、补造明确错误码/版本等 guard 拒绝保留 `status=rejected` 和拒绝原因，不执行工具、不生成新证据、不占成功搜索配额；仍消耗该工具尝试的 step。基础设施失败保留 failed audit，不能伪装成业务拒绝。

## 步数与时间预算的区别

step 耗尽统一生成程序固定提案：reason=`Agent 执行步数达到上限`，reply=`当前信息或执行限制不足以可靠处理，请转交人工核查。`，不含未审查的模型内容或引用。该提案仍等待人工审核。

首次 Judge FAIL 后已无 step 执行 Repair，也使用同一固定提案，清除旧 `candidate_proposal` / `judge_result` / `guardrail_feedback`，设置 `step_limit_reached=true`，保留 `repair_attempt=0`，零 Repair 模型、零第二 Judge 调用。repair 节点直接路由 review / standalone END；pending output 校验固定程序内容，失败 Judge 不作为通过证据。失败候选的真实 Judge 调用仍在 usage 审计中。

时间预算耗尽仍进入失败，不生成预算转人工提案。协议/权限/确定性索引配置错误仍安全失败；第二次 Judge FAIL、Repair 非法动作/来源、Repair 超时等既有边界保持生效。

## Docs 权威正文、导入与索引

本次示例为 `data/synthetic/docs/export-guide.md`、`data/synthetic/docs/import-guide.md`，合计 **2 篇 / 6 个 chunk**，文件及证据均明确标注 synthetic。覆盖示例协作台的导出日期/编码和 CSV 导入预览/写入边界，不代表真实产品的规则或承诺。

PostgreSQL 新表：`docs_datasets`、`documents`、`document_chunks`。目录导入为 additive：新增文档、替换发生变化文档的 chunk；不会把目录中消失的整篇文档自动退役。相同文件重复导入不增加 revision；变化更新 hash/revision，并清空变化 chunk 的索引就绪标记。生产文档编辑界面与自动扫描导入不属于本次范围。

Markdown 要求单一 H1 标题，以后续标题形成章节路径，按段落拆块，超长段落再按字符拆分；规范化 CRLF/LF 后文件 hash 稳定。chunk hash 覆盖标题、章节和正文；`chunk_id=<doc_id>:<section序号>:<chunk序号>`，可引用来源为 `docs:<chunk_id>`。

Milvus collection 为 `docs_bm25_<manifest SHA256前24位>`，与 Cases 命名空间独立，清单含 Docs 版本、schema 与中文 BM25 analyzer。搜索只取 ID/hash/version/score；正文由对应版本的 Docs PostgreSQL 表 hydration，不通过 Case repository。版本错误、正文/index hash 不一致、未就绪或已删除 chunk 被过滤并记录诊断；同步读回 hash 后才标记 ready，避免旧索引正文进入证据。

首版 Docs **仅 BM25**，不构建或调用 Embedding。`dense` 返回 `docs_dense_index_not_ready`；`hybrid` 在渠道审计中明确跳过 dense，并以 BM25 返回证据。集合版本、schema、analyzer、索引或返回协议不一致属于确定性终止错误；临时网络/集合缺失类故障保留既有显式恢复边界。

证据包含 `source_id/doc_id/chunk_id/title/section/text/score/docs_version/content_hash/synthetic/mode/rank`；Decision、Judge、run 数据库结果与工作台均使用该快照。工作台展示 Docs 章节/正文，不向 Case `/sources` 接口查询 Docs 来源。

## 初始化与使用

以下命令在 `D:/AnalyzeAgent/app` 运行。现有 Cases 的 seed/reconcile 保持[原初始化流程](knowledge-writeback.md#初始化与运维命令)。新迁移 `e4ad82c7f321` 接在 `d91e6b72a430` 后，本轮已在真实 PG 隔离 schema 执行，尚未替用户修改日常业务 schema。

```powershell
uv sync --locked
uv run --no-sync alembic upgrade head

# PostgreSQL 预导入，不连接 Milvus；此时尚未完成 BM25 就绪
uv run --no-sync python -m ticketmind.documents.cli data/synthetic/docs --version synthetic-product-docs-v1 --no-index

# Milvus 可用后重复导入并 reconcile 独立 Docs BM25 索引
uv run --no-sync python -m ticketmind.documents.cli data/synthetic/docs --version synthetic-product-docs-v1
```

本地 `.env` 配置：

```dotenv
TICKETMIND_AGENT_VERSION=ticketmind-tools-docs-v2
TICKETMIND_DOCS_DATASET=synthetic-product-docs-v1
TICKETMIND_DOCS_RETRIEVAL_MODE=bm25
TICKETMIND_MAX_SEARCH_ROUNDS=2
TICKETMIND_MAX_DOCS_SEARCH_ROUNDS=2
TICKETMIND_MAX_AGENT_STEPS=8
LANGSMITH_TRACING=false
LANGSMITH_PROJECT=ticketmind
```

未导入 Docs 时 metadata 真实显示 `not_ready`；Case-only 启动仍可读取该状态，但选择 Docs 工具需要完成上述准备，不以假 ready 绕过配置。冻结计算期间修改文档/catalog、索引就绪状态或配置会使契约漂移，旧计算不得继续。正常启动仍使用 `start_v2_flash.cmd`（基础设施已运行时带 `--skip-infra`），或既有 `scripts/start_local.py`；运行真实模型仍需原调用授权。

## 冻结契约与恢复

新计算为 `state_version=2`，`DECISION_PROTOCOL=semantic-guardrail-decision-v3`、`JUDGE_PROTOCOL=semantic-guardrail-v3`。冻结契约含 Docs version/status/catalog_hash/collection/检索模式、独立配额与共享 step 限制；续算前核对输入、工具审计、来源、计数、版本和配置。旧 compute/state v1 不升级、不重算；旧 waiting-review 的原 output 仍可恢复与审核。

恢复为认领短事务 → 释放业务连接 → 续算图 → 重检业务版本和状态 → 保存结果。启动只分类，reviewer 调用 `/tickets/{ticket}/runs/{run}/recover` 才续算。已 checkpoint 的成功步骤跳过；已有 review/output 的业务保存崩溃可从持久化提案恢复，审核按原 key 重放不调用模型。

未知硬退出的未完成 Decision/Judge/Repair **模型调用**仍按冻结 effective timeout 保守扣减，`conservative/estimated` 与已观察时间分账，经 durable `resume_gate` 防重复扣减。未知 bootstrap/search 复合调用仍拒绝续算。若待执行 Decision/Repair 已达 step 上限，它只做固定程序转人工，恢复不创建模型 timeout 估算或虚构模型 attempt；原真实 active time 仍校验。真实 PG 故障注入验证了累计 89/90 秒下该分支可恢复，累计 90/90 秒下仍失败。

## LangSmith 追踪

直接依赖和 lock 均固定 `langsmith==0.12.2`；使用已有 SDK，未新增其他依赖。默认关闭；启用需要 `LANGSMITH_TRACING=true` 和 `LANGSMITH_API_KEY`，项目名取 `LANGSMITH_PROJECT`。关闭或无 key 时不构造 SDK client、不发送网络请求。

自定义事件记录 bootstrap/search 的实际候选、工具拒绝、Decision/Judge 输入输出、Repair、预算 fallback、review interrupt/resume；metadata 用业务 run_id/thread_id、attempt、checkpoint、模型/协议和证据版本关联。Repair 无 step 的事件标为 repair 节点预算 fallback，不伪造 LLM span。Trace 数据不进入 checkpoint、恢复契约或预算。

256 项有界队列、单 daemon worker 与 SDK 单次 HTTP timeout=1000 ms 实现异步发送。SDK/network/队列/thread 启动失败均不传播到业务；队列满或进程硬退出可能丢失事件。SDK 可能自行重试，不能把该 timeout 宣称为整个后台上报耗时保证。工单与证据文本启用后会发往 LangSmith，代码遮蔽常见凭据格式，不保证任意秘密文本均自动识别。当前没有云端凭据，本轮仅验证开关、故障降级、层级及恢复关联，没有云端实连结论。

## 验证证据与复现

最终证据目录：`D:/AnalyzeAgent/refactor-verification/final-tools-docs/`。

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| Tool/Docs、预算、离线 M4、Trace 定向 | 77 passed / 1 skipped | `targeted-final.xml`（最后 Docs fatal 分类修复前） |
| 真实 PostgreSQL 恢复 + Trace | 63 passed / 0 failed | `recovery-final.xml` |
| Docs 确定性失败分类定向 | 23 passed / 0 failed | `docs-fatal.xml` |
| 最终全量离线 | 288 passed / 179 skipped / 0 failed | `offline.xml`、`offline.log` |
| 最终全量真实 PostgreSQL | 465 passed / 2 skipped / 0 failed | `postgres.xml`、`postgres.log` |

离线/真实 PG 门禁覆盖新工具路由、独立 quota、拒绝后 Decision、来源与旧协议拒绝、Docs 导入幂等/正文/版本/hash/namespace（真实 PG + 索引替身）、审核幂等、恢复不重复成功能力、配置漂移、预算 gate、业务保存崩溃、运行取消和本机 SDK 故障路径。离线 M4 `prepare`/`score` 取纯协议常量，不构造 runtime metadata 访问默认 DocStore；实际 HTTP 评测 runner 传入隔离 factory，不读取真实业务 Docs。缓存/acceptance 适配器也允许本次实际 Docs 引用，新增调用额度与旧账本规则未放宽。

```powershell
Set-Location D:/AnalyzeAgent/app
Remove-Item Env:TICKETMIND_RUN_DB_TESTS -ErrorAction SilentlyContinue
Remove-Item Env:TICKETMIND_RUN_MILVUS_TESTS -ErrorAction SilentlyContinue
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=D:/AnalyzeAgent/refactor-verification/final-tools-docs/tmp-offline-final --junitxml=D:/AnalyzeAgent/refactor-verification/final-tools-docs/offline.xml

$env:TICKETMIND_RUN_DB_TESTS='1'
# 如需独立数据库，可配置 TICKETMIND_TEST_DATABASE_URL；测试始终创建 tm_test_<UUID>
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=D:/AnalyzeAgent/refactor-verification/final-tools-docs/tmp-postgres-final --junitxml=D:/AnalyzeAgent/refactor-verification/final-tools-docs/postgres.xml
Remove-Item Env:TICKETMIND_RUN_DB_TESTS -ErrorAction SilentlyContinue

# 离线评测准备，所有新调用累计 ceiling 默认 0
uv run --no-sync python scripts/evaluate_m4.py --stage prepare --output D:/AnalyzeAgent/refactor-verification/final-tools-docs/m4-prepare.json
```

常规复现也可用 `scripts/verify_project.py` 或 `--db`，其证据保存至 `data/cache/verification/<时间-UUID>/`。本轮没有设置 `TICKETMIND_RUN_MILVUS_TESTS=1`；真实 PG 的两个 skip 是可选真实 Milvus 测试。静态语法检查已对 105 个 src/scripts Python 文件使用内存 compile，五动作与两工具白名单检查通过；示例两篇/六块为实际解析计数。首次 `compileall` 受既有 pycache ACL 限制，改用不写 pycache 的内存编译完成检查，没有更改 ACL。

`git diff --check` 通过。最终离线耗时 5.72 秒，真实 PG 耗时 113.42 秒；过渡报告 `postgres-before-docs-fatal.xml` 是最后 Docs fatal 分类修复前的门禁，不作为最终代码的验收证据。最终测试中的两个 warning 来自现有 DashScope / langchain-community 废弃提示。结果证明代码与事务/恢复行为，不能当作真实模型理解、工具选择或证据使用的质量指标。

剩余验收仅涉及本轮未授权/暂缓的外部路径：真实 Milvus 的 Docs BM25 创建/搜索/读回，具备凭据后的 LangSmith 云端写入，以及在原额度内的真实模型决策评估。Docs dense/hybrid 双通道能力尚未实现，不能标记为已验收功能；本轮也未将新增 schema/文档导入到日常业务数据库。
