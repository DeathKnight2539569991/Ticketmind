# M4：A 档评测与可靠性证据

知识存储重构兼容说明（2026-09-17）：正式 runtime 已改为 PostgreSQL hydration；M3/M4 冻结评测仍显式注入原 CorpusSnapshot，保留内容 hash、集合 manifest、证据字段和模型请求指纹。该适配器只用于评测，不是正式 API 的 fallback。本报告原始数据与模型质量判断未重写；新存储验收见 [Knowledge 报告](knowledge-writeback.md)。

日期：2026-09-15。起点 `abe0749`，初始工作区干净；实际确认M3已推送。本次按用户要求仅提交M4，提交/推送状态以Git记录为准，未部署。中断的M5补丁未落盘，API、依赖与锁文件无M5改动。

**用户批准的M4 A档已完成，质量限制保留。** 51条查询完成三模式真实检索；6条validation完成真实Agent评测，动作匹配5/6，3条草稿虚称已转交。实际新增请求65/81次。剩余33条Agent样本（包含全部21条test）未执行，不宣称完整独立test的Agent质量通过；M5本次不推进。

## 数据与使用流程

1. 阅读[合成业务规则](synthetic-business-rules.md)和[72条标签清单](m4-label-review.md)。39条新候选输入为validation 18/test 21，14组不跨split；33条原开发输入只补标签overlay，原v2/M3文件不变。
2. `evaluate_m4.py` 默认只校验文件、审核hash与精确向量缓存，不访问模型、数据库或Milvus。标签缺失、null布尔值、未知来源、跨split同组和重复输入明确失败。
3. `--stage retrieval` 使用同一检索实现查询真实Milvus；BM25无需向量，Dense/Hybrid缺缓存时逐模式记为`not_run`。正式已审指标与待审探索值分开，无相关来源不进Recall/MRR分母。
4. 明确[新调用范围](m4-call-budget.md)后，先生成固定向量，再运行固定Agent样本。新台账默认四类上限为0；成功即缓存，失败计次、不重试、不清空额度。真实Agent入口走隔离PostgreSQL/checkpointer/ASGI HTTP并核对数据库，停在待审核。
5. `--stage score --predictions ...` 可离线重算同配置预测。入口默认读取数据目录相邻标签审核文件，也可通过`--label-reviews`指定；`--adjudications`加载绑定标签hash和原始预测hash的语义判断。用户明确委托Agent审查，本轮72条标签及6条原始输出均记录为`user_delegated_agent`，不是独立人工标注。缺少审查时语义指标留空。

标签文件不进入生产Agent输入、模型提示词、工具返回或索引。业务规则草案也未改写现有提示词；本轮未调整BM25、RRF、Dense参数或增加reranker。

## 真实检索结果

最新完整配置、查询、来源排名、原始分数、候选和耗时见[m4-reviewed-retrieval-results.json](m4-reviewed-retrieval-results.json)。原文由语料版本及source_id回溯，报告省略重复证据文本。[当前预检](m4-reviewed-preflight.json)包含新标签hash及51份可复用向量的状态。授权前的[m4-retrieval-results.json](m4-retrieval-results.json)和[m4-preflight.json](m4-preflight.json)保留为历史快照，不能与修订后标签混算。

固定语料版本为 `synthetic-v2-e5b5a59a7e1481ad3b095d518772354155d891e51ad2a367cf5f7be26540228f`；12条文档，text-embedding-v4/1024维，top_k=5、candidate_k=20、RRF k=60。保留旧集合，本轮只读查询既有M3集合。

| 范围 | 每模式执行 | 受托审查的相关来源分母 | 三模式各自Hit@5 | 三模式各自Recall@5 / MRR@5 | 无足够作答证据 |
| --- | ---: | ---: | ---: | ---: | ---: |
| M4 validation | 18 | 9 | 9/9 | 1 / 1 | 12 |
| M4 test | 21 | 12 | 12/12 | 1 / 1 | 17 |
| M3开发诊断 | 12 | 8 | 8/8 | 1 / 1 | 6 |

153组三模式查询均无服务失败。39条新输入仅10条认为证据足以支持低风险建议，另11条有相关来源但只支持核查或人工流程。无相关来源样本不进入Recall/MRR分母。上述reviewed是受托Agent审查口径，独立人类标注分母仍为0。

旧5条缓存查询的15组三模式排名沿用[M3既有证据](m3-retrieval.md)，未混入新测试分母。标签在早期BM25之后、真实Agent之前收紧：已被事实排除的原因改列干扰，而非按召回排名选“相关”。查询、检索参数、提示词均未更改。小语料、合成输入与三组并列高值不能证明Hybrid优于Dense。

新样本由Agent辅助编写，再经结构及来源校验；同一合成语料上的新输入，不是外部数据或盲测。validation/test的场景分布不同（test高风险较多），应分别汇总；39条不是总体业务效果的充分统计样本。若未来根据test结果调提示词/参数，该test需降为开发诊断并另建新集。

## 失败类型与改进依据

1. **错误码相近不支持相同解释**：M4-012是`E_CURSOR_SIGNATURE`且未传游标，BM25第一名仍为004（`E_CURSOR_INVALID`与筛选变更）。其真实Agent未运行，不能把检索干扰率当作模型误答率。
2. **主题相近但根因前提被否定**：M4-014客户端等待60秒、服务端提前响应；首位006讨论客户端等待不足。修订后将006列为干扰，不能照搬10秒设置。
3. **政策无答案仍返回候选**：M4-036询问正式恢复承诺，语料没有权威政策；BM25仍召回006/002/011等。候选非空不是可作答信号，RRF或提高k也不会创造政策来源。
4. **历史恢复成功不等于当前可执行**：M4-035恢复失败`E_RESTORE_LOCKED`首先召回012的人工恢复流程，来源没有该错误的解释或解锁授权。需要核查是否仍提出无依据解锁/覆盖建议；真实Agent尚未运行。
5. **测试端点受环境代理干扰**：新增SDK场景单独运行通过，首次完整回归有3项未到达本地端点。测试专用HTTP客户端禁用环境代理后，确认1次真实loopback发送并通过全量。只修正测试隔离，没有改生产代理行为。

不据上述开发/候选样本强行加reranker：目前主要证据指向适用性、风险和无答案判断，尚无“相关候选存在但排序差导致整体质量下降”的独立Agent证据。

## A 档真实 Agent 结果与失败分析

配置：理解`qwen3.7-flash`，决策`glm-5.2`，Hybrid与当前`m3-retrieval-evidence-v1`协议。固定6例004/006/011/013/015/018均经真实ASGI HTTP/PostgreSQL/checkpointer/Milvus；全部保存waiting_review且幂等重放未增加运行。没有执行人工业务审核、发布或关闭。原始提案与代码最终提案分开保存，不能以落库成功替代语义正确。

| 检查 | 实际结果与含义 |
| --- | --- |
| 原始动作匹配 | 5/6；018应收集缺失诊断事实，实际重检索后转人工 |
| 提案结构合法 | 6/6；仅证明协议结构，不代表建议适用 |
| 引用来源确实存在 | 4/4；其中2个引用不在相关标签内，均来自006的排除/类比说明，不能直接等同错误采信2次 |
| 无答案仍提出解决建议 | 0/4；范围仅这4条无答案validation输入 |
| 漏掉必须转人工 | 0/2；不证明其他风险场景均安全 |
| 必要问题组完整覆盖 | 0/4；011覆盖参数、错误响应及请求ID，但缺时间/频率；018两组均未问。每组须完整满足，不能解释成完全没有有效追问 |
| 全局规则违规 | 3/6；006/015/018均写“已将此工单转交”，实际工单仍open、运行待审核 |

至少三条真实失败已逐例记录：

- **006覆盖请求**：转人工动作正确，但草稿虚称已经转交；引用预览和恢复历史仅用于排除/类比，不能当作覆盖导入的来源依据。
- **015支付超时**：没有指导重复支付，风险判断正确；仍虚称已转交支付团队，并承诺后续联系，而系统未接外部派发。
- **018组织代理**：误判必要事实已齐，主动重检索后转人工，漏问错误响应、发生时间/范围和已有合规诊断；另有已转交的错误状态承诺。保留运行前的追问标签，不改成模型答案。
- **011追问不足、013措辞过度**：前者缺部分必要事实且索取完整参数未提醒脱敏；后者使用实测8秒且限定只读，但“并非查询本身失败”可删去以避免过度概括。与明确违反的3条分别记录。

后续优先改进草稿的状态措辞与缺失事实判断，再另行授权验证；本轮保留原协议和失败证据，没有为了提高分数偷偷重测。原始6份预测在[m4-predictions](m4-predictions/)，逐例受托语义判断在[m4-adjudications.jsonl](m4-adjudications.jsonl)，汇总和实际usage见[m4-agent-results.json](m4-agent-results.json)。两类审查均为用户委托的Agent判断，仍缺独立人工复核。

累计65次成功请求：固定向量51、理解6、决策7、重检索向量1。理解2915 tokens、决策32892 tokens、Embedding共2749 tokens，均来自实际usage，不估算价格。A上限81，B未授权；剩余33条Agent样本明确未运行。

## 工程验证与实际缺口

原137项测试、M3的8个真实HTTP/PostgreSQL/Milvus场景，以及M2的4个实际终止/重启场景保留为历史证据。本轮按缺口新增：

| 新场景 | 依赖与检查 | 结果 |
| --- | --- | --- |
| 模型SDK超时 | 真OpenAI SDK→独立loopback慢响应→真实ASGI/PG，1次发送，无重试，失败保存 | 通过 |
| 模型SDK 503 | 独立错误端点；错误响应原文不泄露至业务HTTP，原消息保留 | 通过 |
| 模型SDK非法JSON | 独立端点返回非法协议数据，明确failed，无提案 | 通过 |
| 真实检查点缺失 | 仅损坏本测试UUID schema中的本测试thread；重复原审核不重算、不发布 | 通过 |
| 已结束图的审核不一致 | 隔离检查点故障注入；拒绝发布，版本和消息数不变 | 通过 |
| 新评测导出入口 | 真实ASGI/PG/checkpointer、合成Agent；数据库一致、幂等、标签不入输入、原始模型输出缺失不冒充成功 | 通过 |

最新全量 **152 passed：108项逻辑/替身 + 44项真实PostgreSQL**，两条既有依赖弃用警告。新增9项评测逻辑测试与6项真实PG场景；测试总数不能当模型成功数。实际进程终止/重启和M3的8场景本轮没有机械重跑，也不计入152。

本地JUnit原始结果位于被忽略的`data/cache/m4/regression.xml`；摘要见[m4-verification.json](m4-verification.json)。测试仅使用随机tm_test_ schema及独立错误端点，没有中断共享服务。默认uv缓存及Python临时写入曾受Windows沙箱权限限制，使用现有`.venv/Scripts/python.exe`和每次新建的仓库临时目录完成验证；没有升级依赖或修改目录ACL。

## 复现

```powershell
uv run --no-sync python scripts/evaluate_m4.py
uv run --no-sync python scripts/evaluate_m4.py --stage retrieval --include-m3-diagnostics
$env:TICKETMIND_RUN_DB_TESTS = '1'
uv run --no-sync python -m pytest -q
Remove-Item Env:TICKETMIND_RUN_DB_TESTS
```

评分示例（预测与审核文件须使用实际产物，不能复制虚构批准值）：

```powershell
uv run --no-sync python scripts/evaluate_m4.py --stage score --predictions <原始预测JSON路径> --label-reviews <标签审核JSONL路径> --adjudications <原始输出语义审核JSONL路径>
```

语义审核每行字段：case_id、label_hash、prediction_hash、reviewer、reviewed_at、review_method、rationale、raw_question_coverage（按必要问题顺序的布尔列表）、raw_forbidden_conclusion_violations（清单违规）、raw_global_rule_violations（G01—G08）。它只评价原始文本；不拿人工改写替代原稿。标签hash同时绑定语料版本。

## 最值得理解的三个决策

1. 为什么线索命中率高，仍可能没有足以支持建议的证据？
2. 为什么审核记录绑定输入/标签和原始预测hash，而不是只存一个reviewed布尔值？
3. 为什么缺向量是not_run、SDK超时是failed，且两者不能进入相同的成功率分母？

剩余边界：33条Agent输入未运行、无独立人工标注或真实客户指标、模型状态承诺与追问不足尚未修复。按用户最新要求，M5不在本次提交范围内，后续开发另行推进。
