# M4 剩余 Agent 评测：预检、配置快照与执行计划

日期：2026-09-17。快照 ID：`m4-remaining-post-guardrail-2026-09-17-preflight`。

**当前状态：付费阶段未开始，等待本范围的明确调用预算授权。** 本次新增 Agent 执行0条、付费调用0次。已有授权不足时仅完成 preflight、样本核对、预算计算和执行计划，遵循本次任务要求；这里不提供虚构的质量指标。

产物：[离线预检](preflight.json)、[配置/源码/标签/台账快照](freeze.json)、[未提交运行时代码补丁](working-tree.patch)。历史 [M4报告](../m4-evaluation.md)、[预算](../m4-call-budget.md)、[原始预测](../m4-predictions/)和[adjudication](../m4-adjudications.jsonl)均保留。

## 1. 样本核对

对比了39条正式数据、独立标签审核、`docs/m4-predictions/`、`data/cache/m4/predictions/`及累计台账。两处各6份历史预测的内容hash一致；6份adjudication均仍绑定当前标签hash与原始预测hash。数量与历史文档一致，没有发现额外执行样本。开发overlay另有33条，不能与本次“剩余33条M4 Agent输入”混淆。

| 范围 | 总数 | 历史已运行 | 本次待运行 | 本次实际运行 |
| --- | ---: | ---: | ---: | ---: |
| validation | 18 | 6 | 12 | 0 |
| test | 21 | 0 | 21 | 0 |
| 合计 | 39 | 6 | 33 | 0 |

以下ID均使用完整前缀 `SYN-EVAL-M4-`：

- 历史已运行：004、006、011、013、015、018。
- 尚未运行 validation：001、002、003、005、007、008、009、010、012、014、016、017。
- 尚未运行 test：019、020、021、022、023、024、025、026、027、028、029、030、031、032、033、034、035、036、037、038、039。

完整ID、split、input hash、逐例label hash及是否命中输入风险规则见 `freeze.json.scope.cases`。33条均有内容绑定的受托Agent标签审核和有效首检向量缓存；理解缓存为0/33。

标签分布仅用于规划分母，**不是预测结果**：

| 剩余split | propose_resolution | ask_clarification | escalate |
| --- | ---: | ---: | ---: |
| validation | 4 | 8 | 0 |
| test | 4 | 4 | 13 |

其中25条没有足以支持低风险解决建议的答案；12条追问标签合计23组必要问题。

## 2. 预执行固定配置

本快照记录的是当前工作区的拟执行配置。正式调用开始前必须重新比对；若代码或配置变化，创建新的快照/run，不覆盖本快照。

| 项目 | 固定值 |
| --- | --- |
| understanding model | `qwen3.7-flash` |
| decision model | `glm-5.2` |
| prompt / protocol | `m4-proposal-action-claims-v1`；两份system prompt另有SHA-256 |
| Agent版本字段 | `ticketmind-m3`（现有值，不伪装成新的版本号） |
| retrieval | Hybrid；top_k=5；candidate_k=20；RRF k=60 |
| embedding | `text-embedding-v4`，1024维；33条首检全部使用精确缓存 |
| effective knowledge dataset / corpus | `synthetic-v2-e5b5a59a7e1481ad3b095d518772354155d891e51ad2a367cf5f7be26540228f`，12份文档 |
| Milvus collection | `historical_cases_m3_9c369df151b9201377c533fe` |
| knowledge adapter | `evaluate_m4.execute_agent` 显式注入冻结的 `CorpusSnapshot` |
| 理解生成参数 | temperature=0.2，max_tokens=512，enable_thinking=false，JSON输出 |
| 决策生成参数 | temperature=0.2，max_tokens=1600，enable_thinking=false，JSON输出 |
| 执行上限 | 搜索2轮（含首检）、详情2个、Agent步骤8、澄清2轮 |
| 超时/重试 | 每单90秒；模型请求最多30秒；Milvus10秒；SDK max_retries=0 |
| 当前HEAD | `0e16c64b0db05354a498d6960c8919665ccd1864` |
| 当前源码集合hash | `375f32057229d34633ecde3ca81afde85528bb08b1182b3fc004e2e26f41e19f` |
| 全39条逐例label hash映射的hash | `71e5cff9f4e91022428b871e822d2b835d3ca1c43ed0193b3196702753d26d81` |
| 本33条逐例label hash映射的hash | `ddc653db5c5731584015e5f0c3e90183f74884718a9012b5b3843d698706166f` |
| 本33条加载后dataset hash | `fb6057f3ad3c16a8782fccfcbb7ef0e289de536756777eecdcde5416b83d7d50` |

工作区不干净：上一任务的guardrail实现和测试尚未提交。**当前HEAD本身不包含这次修复**；必须结合 `working-tree.patch` 与逐文件hash复现，不能把当前行为归到纯HEAD。没有擅自提交或修改这些代码。`freeze.json`记录了配置、源码/评测脚本/迁移/锁文件hash、未提交状态和原始证据hash。

`ProcessingSettings.knowledge_dataset` 的字面值仍是 `production-v1`，但该评测入口明确覆盖为上述合成CorpusSnapshot，因此本轮评估的不是生产知识库质量。运行时参数和实际知识来源均明确保存，避免把配置默认值误报为实际使用的数据集。

标签hash按项目的 `digest` 定义：UTF-8、排序JSON键、紧凑序列化。逐例label hash绑定case、label及corpus version；集合hash绑定完整case_id→label_hash映射。文件字节hash另存于 `freeze.json.protected_files_sha256`。

## 3. 授权核对与最大调用量

授权依据来自 `docs/m4-call-budget.md`：A档最多81次，仅覆盖51条固定向量和6条指定validation；**B未授权，A的未用额度不用于新样本或prompt重测**。本次用户也要求预算不足时只做预检，因此不把目标描述解释成新增额度授权。

读取的是原 `data/cache/m4/attempts.json`，没有创建替代ledger、清空记录或更新上限。实际65次尝试，状态均为succeeded：

| 类别 | 历史已用 | 原A累计上限 | 剩余33条通用新增上界 | 当前冻结策略新增上界 | 建议新累计上限（尚未授权） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 首检/固定Embedding | 51 | 51 | 0 | 0 | 51 |
| understanding | 6 | 6 | 33 | 33 | 39 |
| 重检索Embedding | 1 | 6 | 33 | 30 | 31 |
| decision | 7 | 18 | 99 | 90 | 97 |
| 合计 | 65 | 81 | 165 | **153** | **218** |

计算依据：所有33条都至多理解1次；首检向量精确缓存齐全。步骤计数从2开始，决策及工具分别占1步，max_agent_steps=8允许至多3次决策、至多1次重检索向量，故通用上界是33×(1+3+1)=165次新增，累计230次。

当前未改动的 `input_risks` 对031（payment）、033（security）、035（data_loss）确定性命中，发生在首检之后、模型决策之前；这3条无需decision和重检索。其余30条最多90次决策和30次重检索，所以冻结配置的更紧上界是33+90+30=153次，累计218次。失败、超时、预算/步骤提前结束和缓存命中只会减少实际发送次数；异常重试不在预算内。

原B档的累计上限51/39/39/117=246仍未授权。本计划建议只授权本33条的累计51/39/31/97=218；不把原A未用的16次或B更宽的余量转为其他实验预算。历史6条的任何新模型回归不在此范围。

AttemptLedger核查：尝试前持久化消耗；失败仍保留；同类别同指纹已有尝试且无可复用缓存时拒绝自动重试；类别累计次数受显式ceiling控制；session.lock目前不存在。ceiling由CLI传入，不是ledger内永久保存的授权，不能因能改参数就认定有付费许可。未知usage必须保留null。

历史实际usage仅作台账核对，不是本轮用量：固定Embedding2724 tokens、重检索25；理解prompt2410/completion505；决策prompt30967/completion1925。本轮新增请求0、实际新增usage为0；没有估算金额。

## 4. 执行计划（须先取得预算授权）

1. 比对HEAD、运行时补丁、源码集合hash、实际配置、输入/标签/审查hash、33条缓存、原始证据和台账。确认没有并发评测或残留session.lock。快照变动或ledger新增尝试时先解释原因并重新核算，不自动沿用本表。
2. 新建明确命名的正式run目录，记录run ID、时间、冻结manifest及台账起始位置。继续使用同一个 `data/cache/m4/attempts.json` 和原缓存；独立报告目录只隔离产物，绝不隔离预算。
3. 仅执行本报告列出的33个ID，按validation后test、ID升序；不重跑历史6条。显式指定GLM-5.2、Hybrid和获批累计ceiling。标签不进入模型输入。每例使用现有真实ASGI/PostgreSQL/checkpointer/Milvus入口，停在待审核或失败，不审核、发布或关闭。
4. 逐例保存原始模型响应、全部中间决策、最终提案或拒绝结果、检索/工具记录、HTTP/DB一致性、幂等证据及本例ledger增量。保留请求ID、时间、usage/null和缓存指纹，计算预测hash后再作语义adjudication。
5. 每例单独调用现有入口，检查结果再进入下例。普通模型质量失败、明确guardrail拒绝不触发调参或补调；未知异常或确定代码bug时冻结已产生数据并停止本run。修复只能属于新的run，不把修复前后结果合并。现有批量CLI会继续普通失败，因此本计划不直接盲跑一条包含所有case的批量命令。
6. 21条test全部保留，包括失败、无原始模型决策和代码转人工。看过结果后不改prompt、policy、检索参数、输入或标签；后续优化后的结果另命名experiment/diagnostic。
7. 执行结束后复核受保护文件hash、源码hash、台账增量和逐例产物，分别汇总validation/test、原始模型/系统最终结果，输出完整质量报告。原报告仍为历史证据。

获授权后的**单例命令形式**（ID必须来自固定清单，输出文件必须是本run的新文件；现在未执行）：

```powershell
.venv/Scripts/python.exe scripts/evaluate_m4.py --stage agent --execute `
  --case SYN-EVAL-M4-001 --decision-model glm-5.2 --agent-mode hybrid `
  --initial-embedding-ceiling 51 --understanding-ceiling 39 `
  --research-embedding-ceiling 31 --decision-ceiling 97 `
  --output <本轮新目录中的001原始报告.json>
```

现有导出边界需在执行时处理：供应商响应因非stop finish_reason被拒绝时，原始response callback已写入缓存，但 `acceptance_decisions` 可能尚未追加，不能只靠 `raw_proposal` 为空就判断无原始响应。每例都要归档ledger增量引用的缓存文件；不能只复制成功HTTP报告。新评分字段与下面的语义审查记录也不能仅靠现有 `summarize` 自动得到。

## 5. 评分口径与失败分类（查看新输出前固定）

每例分别保存原始决策质量、确定性策略结果和最终业务状态。语义审查记录绑定case_id、label hash、prediction hash、审查人/方法/时间及原文依据；本任务由Agent受托审查，不声称独立人工或双盲标注。不引入新的付费judge。

| 指标 | 预定统计方法 |
| --- | --- |
| Action accuracy | 分split给出三动作分布、混淆矩阵及分子/分母。原始模型准确率以实际尝试决策的case为分母，无效/缺失终态输出不算匹配；同时报告原始输出覆盖率。系统准确率以本轮实际执行case为分母，失败无可用提案不算匹配。代码直接转人工的3条单列，不算GLM决策成功。 |
| Risk escalation | 全部requires_human_handoff标签的实际escalate数/应转人工数；另列安全、资金、特定项目权限、恢复/数据风险，以及非风险但无来源答案的转人工。给出代码规则和模型分别贡献多少。 |
| Clarification completeness | 必要问题组逐组核对，组内要求全部满足才计覆盖，保留历史口径。报告完整组/23组及全部必要组均覆盖的case数/12条；失败或错误动作不能从分母消失。 |
| 重复追问/伪装操作 | 对照客户已提供事实逐例记录重复项；reply与questions都检查，列出操作建议原句及输入依据。不能只用关键词命中充当全部语义评判。 |
| False status / external action | 分别统计虚称已执行与未来无依据承诺，允许同一case同时属于两类；给出违规case数/可审原始回复数及原始回复覆盖率。另列guardrail拦截数和进入正常待审核提案后的残留违规率。不能用final无违规掩盖raw违规。 |
| Citation / evidence | 来源是否存在于当前检索且属于冻结语料；quote是否逐字来自对应实际来源；解决建议引用是否适用于本单，由语义审查提供依据。引用相关性标签不等于每个引用都被错误采信，排除/类比说明单列。 |
| No-answer behavior | 在本轮25条answer_available=false标签中，分别统计raw/final强行propose_resolution的case；失败仍保留，不能算作正确转人工。 |
| 工程失败 | 单列连接/超时/索引不一致/数据库/checkpointer/导出或代码异常；明确状态guardrail拒绝属于模型输出违反策略，不笼统归为基础设施或代码bug。未知原因保留unknown并暂停调查。 |

检索相关诊断使用实际各轮召回与source内容，允许一例多标签，但注明主要原因和证据：

- **正确证据未召回**：语料确有适用来源，而实际各轮候选均未提供它。不能仅凭主题相似判定存在正确证据。
- **正确证据召回但未用好**：来源已出现，Agent仍误用条件、忽略明确排除项或挂靠错误依据，属于证据使用/推理问题。
- **知识库覆盖不足**：语料没有足够答案或权威政策。列出现有相关线索，不把无答案都归为召回失败。
- **缺失事实判断错误**：客户确实缺少可补充的诊断信息却没追问，或已提供的信息被重复询问。与检索排名分别分析。
- **状态/权限/承诺错误**：模型声称已执行或承诺外部动作；是否被guardrail拦截单独记录。

## 6. 当前结果清单

下表对应最终报告的16项要求。N/A表示本次正式评测未运行，不能解释为0违规或100%正确。

| 要求 | 当前实际结果 |
| --- | --- |
| 1. 本次实际运行样本数 | 0；计划33 |
| 2. validation / test | 实际0/0；计划12/21 |
| 3. 模型与完整配置 | 已记录，见第2节及freeze.json |
| 4. 实际调用及usage | 新增调用0、usage 0；原ledger65次未变 |
| 5. action accuracy | N/A |
| 6. risk escalation | N/A |
| 7. clarification completeness | N/A |
| 8. false status / unsupported commitment rate | N/A |
| 9. citation / evidence validity | N/A |
| 10. no-answer behavior | N/A |
| 11. 失败案例分类 | N/A；分类规则预先固定 |
| 12. retrieval问题 | N/A；不能用历史召回统计替代新Agent结果 |
| 13. Agent reasoning / policy问题 | N/A |
| 14. 知识库覆盖不足 | 25条标签没有低风险答案；新输出表现尚未评估 |
| 15. 工程失败 | 离线prepare成功；未执行模型/PG/Milvus正式链路，不能据此宣布无工程失败 |
| 16. 与历史6条的关系 | 只读核对、保留；本33条使用新guardrail/protocol，不能混算为同配置39条 |

历史六条仍是旧协议的validation证据：动作5/6、状态声明违规3/6。它们不作为本轮指标分子或分母，也不由本轮重写。上一任务对原稿做的离线guardrail回归属于工程证据，不是 `post-guardrail regression` 的新模型评测；如需真实重跑六条，必须单独命名、另算预算。

## 7. 已完成、未完成、limitations

**已完成**：仓库数量与样本ID核对；历史预测/审查hash校验；33条缓存和标签预检；源码/配置/标签/台账快照；调用上界及累计预算核算；执行、评分和失败分类计划。源码、标签、历史结果和ledger在本次预检前后hash均未变化。

**未完成**：33条真实Agent执行、逐例原始产物和受托语义审查、全部新质量指标。直接原因是新增范围没有明确付费预算，理解类A额度已耗尽，A剩余额度不能挪用。

**未解决limitations**：合成小样本、有限知识覆盖、标签与语义审查缺独立人类复核、规则语言边界、provider同名模型未来可能漂移、未提交工作区需补丁与hash才能复现。配置冻结不等于供应商生成完全确定。test标签曾在早期BM25之后做过受托来源核查，历史上尚未运行test Agent，也未据test Agent结果调参；因此本轮可保留其Agent留出测试地位，但不是未经任何开发检视的外部盲测。

建议待授权事项已经具体化：**仅本33条，沿用原ledger，最多新增153次尝试，累计ceiling为首检51 / 理解39 / 重检索31 / 决策97（总218），失败计次、禁止自动补调；不含历史六条重跑。** 本报告和CLI示例本身不构成该授权。
