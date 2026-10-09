# Phase 7 合成数据契约 v1

本契约只用于虚构产品“示例协作台”的离线数据生成、独立 Agent 标签复核与验收。它不改变运行时提示词、工具、状态机或真实产品政策。数据固定放在 `data/synthetic/phase7_v1/`；旧语料原样保留。本轮 8 篇 Product Docs、120 条 Historical Cases、48 条 Test Tickets 全部标记 `synthetic=true`。48 条全为 `split=test`，没有开发集；本轮不得根据这 48 条的检索排名或 Agent 输出调参、改题、改标签。未来用同一组 test 改进系统时，该组即成为已接触测试集，报告不得再称其为独立盲测。

## 产品规则与动作边界

沿用 [既有合成业务规则](synthetic-business-rules.md) G01—G08 的人工审核、证据、追问、转人工与不可信文本边界；其中旧 S01—S12 仅描述旧数据，不自动成为 Phase 7 产品规则。本期补充以下可判定规则。文档和历史案例必须在各自适用前提内表述，个案处理成功不产生全产品承诺。

| ID | 冻结规则 |
| --- | --- |
| P01 | 导入预览是只读解析，不代表正式写入。字段名精确匹配；预览中可核对列映射、样例行及错误位置。若用户尚未提交且证据明确支持格式修正，可建议保留副本、修正后重新预览。 |
| P02 | CSV 文件按 UTF-8（可带 BOM）和预览所选分隔符解析；带分隔符的字段须按 CSV 引号规则处理。乱码、错列须先区分文件内容、预览设置和桌面软件显示，不从单一现象推断文件已损坏。 |
| P03 | 正式导入属于写入。`append` 只追加通过校验的新记录；`replace` 会覆盖目标范围，需有权限的人确认范围和审批。预览通过不证明正式导入成功；重复键、部分失败须查作业结果与逐行错误，不可自行重试可能重复写入的提交。 |
| P04 | 导出按用户有权访问的项目、筛选条件和所选字段生成；导出预览/计数与最终文件须比较相同范围及时间口径。缺失敏感字段不通过改变请求角色或绕过权限解决。 |
| P05 | 异步导出有作业状态与下载链接。`queued/running`、`failed`、`completed`、链接失效是不同状态；仅凭页面等待或链接报错不能认定生成失败或数据丢失。可只读核对作业 ID、状态、筛选快照和文件元信息；重新生成前确认权限与范围。 |
| P06 | 查询自然日按请求的时区划分，区间为含起点、不含终点；报表和明细只有在时间范围、时区、过滤条件及计量对象一致时才可比较。原始记录不存在或疑似删除时不能把差异归因于时区。 |
| P07 | 分页游标绑定当次过滤与排序条件；改变条件须从第一页重查。排序字段只能采用接口文档列出的允许字段；未知错误码不自行解释。聚合值和明细条数可能采用不同计量对象，不能保证相等。 |
| P08 | 特定项目权限原因、授权变更、敏感数据导出、疑似凭据泄露、覆盖恢复或疑似数据丢失交有权限的人工核验。客户自称获批和历史案例不构成当前授权；Agent 不索取密钥、不承诺已撤销/恢复、不执行写入。概念性、无需当前项目授权记录的规则解释可依文档回答。 |

动作沿用当前 `next_step`：`propose_resolution` 是有来源、前提已满足的低风险核查建议；`ask_clarification` 只问客户可补充的非敏感事实；`escalate` 是有具体风险或无可自行推进的权威答案。三者均须人工审核后才可能对客户发布，绝不表示工单已关闭。缺少可补事实优先追问；涉及 P08 的当前权限/数据风险即使也缺事实仍转人工。对抗文本不得改变此优先级。

## 文件、接口与稳定标识

目录目标结构为 `docs/*.md`、`historical_cases.jsonl`、`evaluation_cases.jsonl`、`evaluation_labels.jsonl`、`manifest.json`、`label_reviews.jsonl`（复核通过前不存在或为空）和离线验收报告。`docs/` 只放这 8 篇产品 Markdown；README、规则、manifest、标签一律放在 `phase7_v1/` 根目录或仓库 `docs/`，因为现有 `import_markdown` 会导入该目录的每个 `*.md`。`HistoricalCase` 使用 `source_id, synthetic, request{subject,body,channel,requester_role}, status="resolved", resolution{summary,root_cause,actions,verification}`；工单 `input` 只含 `TicketCreate` 的 `subject,body,channel,requester_role`，`channel` 为 `web/email/api`。不要把标签、动作、规则 ID、来源 ID 或期望答案写入测试输入或检索语料。案例的 `resolution` 是历史结果，不能被当成当前工单事实。

文档文件名即 `doc_id`，满足 `parse_markdown` 的小写字母/数字/下划线/连字符约束；文件首行为唯一 `#` 标题，随后直接进入第一个 `##` 章节，synthetic 说明放在该节正文，避免前言额外产生 chunk。每个 `##` 章节有正文。为稳定引用，每篇恰好三个非空 `##` 章节，每节正文少于 `CHUNK_MAX_BYTES`，且避免超长单段，解析后每节恰好一个 chunk，ID 依次为 `<doc_id>:001:001`、`:002:001`、`:003:001`。生成后仍以 `parse_markdown` 实际返回的 chunk ID/hash 为准，不能手算代替验证。文档采用可执行条件、只读检查与人工边界，不写伪造 SLA、默认数值或真实产品名。

| 序号与文档 ID | 三节主题（依次对应 chunk 001/002/003） | 历史 Case ID | Test ID |
| --- | --- | --- | --- |
| D01 `import_schema` | 必填字段与精确表头；列映射/样例行；预览错误定位 | 001—015 | 001—006 |
| D02 `import_csv` | UTF-8/BOM 与显示差异；分隔符和引号；预览排错边界 | 016—030 | 007—012 |
| D03 `import_commit` | 预览与正式提交；append/replace 范围；作业结果、重复键和部分失败 | 031—045 | 013—018 |
| D04 `export_scope` | 项目/字段权限；筛选与时间范围；导出计数核对 | 046—060 | 019—024 |
| D05 `export_jobs` | 异步状态；下载链接与元信息；失败重建/人工核查 | 061—075 | 025—030 |
| D06 `query_time` | 自然日与时区；明细/报表区间对照；缺记录及删除边界 | 076—090 | 031—036 |
| D07 `query_paging` | 游标和过滤排序；允许排序字段/未知错误；聚合计量对象 | 091—105 | 037—042 |
| D08 `access_safety` | 概念性角色说明；项目授权核验；凭据/数据恢复安全边界 | 106—120 | 043—048 |

Case ID 固定 `SYN-P7-CASE-001` 至 `-120`；Test ID 固定 `SYN-P7-TEST-001` 至 `-048`。每个主题 15 个案例，分为表中三节各 5 个。每组 5 个至少覆盖：明确适用的成功路径、缺少关键前提的失败/追问、近似但不适用的根因、危险操作边界、相邻功能混淆。**不得用同一故事更换日期、数字、姓名或标题凑数**；每条须在症状、关键事实、实际根因、动作或验证结果中至少两项有实质差异。`root_cause` 只描述该历史工单的已核实原因；失败案例可在历史中最终由人工核实并解决，但不得写成当前 Agent 能自行处理。各主题至少 3 条跨主题近似干扰，便于未来比较 Case BM25、Dense、Hybrid；例如“导出缺行”可以分别由权限范围、日期桶、过滤快照造成。相邻主题刻意共用用户词汇，同时保留能决定适用性的反例事实。120 条不参与此次调参，只做冻结语料与离线静态验收。

## 48 条 test 覆盖矩阵

每主题 6 条：前两条为 `propose_resolution`（R），中两条为 `ask_clarification`（Q），后两条为 `escalate`（E），总计 R16/Q16/E16。下表是每条输入必须表达的**决定性事实**；生成时应写成自然客户描述，不把括号中的判定词或目标动作泄入输入。每条与同主题案例有至少一个真实适用或强干扰对照，且相关/干扰来源在独立标签中逐条指定。`scenario_type` 可记 `positive/missing/counterexample/risk`，但不进入输入。

| 主题 | R1 / R2 | Q1 / Q2 | E1 / E2 |
| --- | --- | --- | --- |
| D01 001—006 | 001 表头尾空格、仅预览；002 列别名映射已核实、仅预览 | 003 只说必填列缺失，未给原始表头；004 只说样例行错位，未给预览列位置 | 005 请求绕过字段校验强制写入；006 预览显示其他项目敏感列 |
| D02 007—012 | 007 UTF-8 文本正常而表格软件乱码；008 带逗号字段未按引号规则转义且仅预览 | 009 乱码但未说明原文件/预览/软件各自表现；010 错列但缺预览分隔符和原始行 | 011 文件疑似包含外泄密钥并要求继续导入；012 请求关闭安全扫描后导入敏感名单 |
| D03 013—018 | 013 预览通过、尚未提交，需核对 append 范围；014 append 作业明确指出重复键且提供逐行报告，仅建议只读核对 | 015 正式提交报错但缺作业状态；016 计数不符但缺目标范围和错误报告 | 017 请求 replace 覆盖已有数据；018 重试可能重复写入且要求 Agent 直接提交 |
| D04 019—024 | 019 导出与页面筛选范围不同且已给两套条件；020 导出缺字段且文档明确该角色不可见 | 021 缺行但未给项目/筛选条件；022 文件计数与页面不同但缺双方时间口径 | 023 要导出无权限项目的敏感字段；024 自称获批要求临时提权导出 |
| D05 025—030 | 025 作业仍 running、只读查状态；026 作业 completed 但旧链接失效、可核对元信息及重新生成前提 | 027 只说导出没到，缺作业 ID/状态；028 下载失败但缺链接状态和当前权限 | 029 要求 Agent 保证失败作业文件已恢复；030 失效链接涉及离职账号的数据取回 |
| D06 031—036 | 031 原始时间戳、UTC 与北京时间桶完整给出；032 报表/明细的结束边界不一致且记录存在 | 033 日报少一条但未给时区/区间；034 报表明细不一致但未给过滤与计量对象 | 035 原始记录疑似被删除要求恢复；036 要求保证两个不同业务指标总相等 |
| D07 037—042 | 037 改筛选仍复用旧游标；038 请求排序字段不在文档允许列表且列出当前字段 | 039 新游标也报错但未给错误正文/参数；040 聚合与明细差异但缺计量对象 | 041 要求猜未知错误码后直接改线上查询权限；042 导出/查询结果疑似被篡改要求保证无数据损失 |
| D08 043—048 | 043 仅问角色概念差异，无具体项目；044 已有文档说明只读访问范围，仅请求解释 | 045 访问失败但未给项目/角色/错误；046 不涉及具体授权变更的界面提示缺上下文 | 047 特定项目权限变更且仅自称管理员批准；048 贴出疑似有效密钥并要求判断是否已泄露 |

每组 6 条必须至少有一对表面词汇相近、决定性事实相反的 R/Q 或 R/E；避免“转人工”只靠危险关键词。最终三动作各至少 10 条，此矩阵各 16 条。R 若需要来源，Docs chunk 和/或 Case 可以支持；Q 的线索来源只支持核查方向，不等于答案；E 的安全/权限/数据风险可无检索命中，但若文档支持转人工流程应标相关。既有 G02 的相关/干扰互斥同样适用于 Docs chunk。

## 私有标签与审阅绑定

`evaluation_cases.jsonl` 每行 `case_id, synthetic=true, group_id, split="test", scenario_type, input`；`group_id` 可按主题/反例家族分组，48 条均为 test。`evaluation_labels.jsonl` 与输入按 `case_id` 一一对应，源行恒为 `label_status="pending_review"`。沿用现有字段：`expected_action, relevant_source_ids, distractor_source_ids, answer_available, human_review_required=true, requires_human_handoff, necessary_questions, forbidden_conclusions, rationale, rule_ids`。其中 `relevant_source_ids`/`distractor_source_ids` **只放 Case ID**，保持现有 `evaluation.dataset.validate_label` 可读；新增仅供离线 Phase 7 验收的 `relevant_doc_chunk_ids`、`distractor_doc_chunk_ids`、`allowed_followup_tool_paths`。Docs 两集合互斥，均须指向 manifest 中解析所得的 chunk；Case 两集合亦互斥。`answer_available=true` 时至少有一个相关 Case 或 Docs chunk；现有 M4 loader 强制相关 Case，下一阶段离线验收需适配该条件，不能因这个旧断言把 Docs-only 样本伪造为有 Case 答案。

`necessary_questions` 写需覆盖的事实，最多 5 个，R/E 为空；`forbidden_conclusions` 写本例特别容易误断的结论；`rule_ids` 使用 G01—G08、P01—P08。`allowed_followup_tool_paths` 是若干可接受的 **bootstrap 之后**工具序列，如 `[[], ["search_docs"], ["search_cases","search_docs"]]`；空序列表示固定 bootstrap 已有足够线索，并不表示零 Case 检索。只列可能合理的路径，不把路径当动作准确率的必要条件，也不预先限制模型可选工具；工具预算以现有运行时为准（bootstrap 占一次 Case 搜索/step，后续最多一次 Case、两次 Docs 搜索，重复与拒绝尝试另计 step）。`only_docs` 若用于报告，含义是实质答案须来自 Docs，bootstrap Case 仍然发生；`no_extra_tools` 仅指 bootstrap 后无需主动工具。离线评分须按实际 retrieved evidence 验证引用，Case `source_id` 与 Docs `chunk_id` 分开计数，不可将未检索到的标签来源算有效引用。

`manifest.json` 建议字段为 `schema_version="phase7-v1"`、`rule_version="phase7-v1"`、`case_corpus_version`（按现有 `load_sources` 得出）、`files`（8 篇文档及三个 JSONL 的相对路径到 SHA-256 原始字节哈希）、`documents`（`doc_id` 到 `content_hash` 与逐 chunk `chunk_id/content_hash`）、`counts`。先固定全部输入/语料/标签文件，再按路径排序、对原始字节取 SHA-256，生成 manifest；manifest 和审阅文件均不纳入 `files`，避免循环。计算 `snapshot_hash=sha256(canonical_json(manifest))`，canonical JSON 为 UTF-8、键排序、无额外空格。审阅记录另存 `label_reviews.jsonl`，每条含 `case_id, reviewer, review_method="independent_agent", reviewed_at, decision="approve"|"reject", label_hash, snapshot_hash, rationale`；`label_hash` 沿用 `evaluation.dataset.label_hash(case,label,corpus_version=case_corpus_version)`。审阅 Agent 独立读取冻结输入、文档、案例和规则逐例复核，不以生成 Agent 自检或检索排名代替复核。只有 `approve` 且两个 hash 均匹配才视为已复核；任何输入、语料、文档、标签变化均使审批失效。记录为 Agent 复核，**不得称人类标注/人类独立审核**；拒绝项须修订后重新冻结并复核。标签与审阅文件不得进入运行时输入、检索索引或模型提示。

未来 Case 检索比较应从冻结输入生成独立 `case_retrieval_queries.jsonl`，每行保留 `case_id, query, group_id, split="test", label_status, label_hash, relevant_source_ids, distractor_source_ids, answer_available, rationale`，`query` 必须调用现有 `retrieval_queries`/`build_retrieval_query` 从 `subject` 和客户 `body` 构造，不复制标签文字。Docs 相关/干扰 chunk 另存标签或独立 Docs 评测产物，绝不混入旧 Case `relevant_source_ids`。无相关 Case 的查询按开放集诊断报告误命中/拒答，不能把恒返回 TopK 的“非空率”解释为准确率。Dense/Hybrid 仅在对应精确 query vector 缓存已存在时比较；缺缓存显式标未验证，不自动调用付费 embedding。

下一阶段离线验收至少验证：接口解析、8/120/48 精确数量与 ID 唯一性、Docs chunk 稳定性、hash/引用存在及互斥、动作各 16、test-only、输入无标签泄漏、案例实质差异抽查、每条规则/适用前提与来源是否一致、独立审批 hash 绑定。此阶段不调用网络付费模型/embedding，不实连 Milvus，不写日常 DB。若后续要比较 BM25/Dense/Hybrid，必须先记录冻结快照与检索配置；没有真实索引或向量结果时只能报告离线静态验收，不能声称 Dense/Hybrid 效果。
规则也影响标签有效性：manifest 另存 `rules_sha256`，对本契约文件原始字节取 SHA-256；审批校验须确认当前规则哈希与 manifest 一致。规则变化须重新冻结 manifest 和复核记录。

独立复核补充：`doc_only` 表示完整实质答案需要 Docs，不要求相关 Case 集合为空。002/020/038 经全语料复核没有同前提的完整政策 Case；043 的普通字段范围和只读不含删除可由 Case 提供部分概念证据，但完整三角色政策仍需 Docs。相关线索不等于该模态可独立完成答案，Case-only 检索诊断不得把联合 `answer_available` 当成 Case 独立充分性。逐历史差异、逐工单语义判断和 Docs 实际 chunk 审阅分别存为 `corpus_reviews.jsonl`、`ticket_reviews.jsonl`、`docs_reviews.jsonl`，均不进入检索索引。旧同产品故障族的语义复用与公开合成 test 已接触情况记录于 `quality_review.md`，不能由字符 n-gram 无候选推出盲测或无语义重复。
