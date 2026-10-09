# Phase 7 合成候选数据

synthetic=true；虚构产品“示例协作台”，不是真实客户或真实产品政策。

8 篇产品文档，每篇三节；120 条已解决历史个案，每节五条；48 条工单全部 split=test，建议/追问/人工各16条。输入仅TicketCreate四字段，标签与答案只在独立文件。历史结果只描述该个案，不能当新工单事实。

运行 `python data/synthetic/phase7_v1/build_dataset.py` 可从静态定义确定性重建本目录候选数据，无网络、模型、embedding、数据库或Milvus调用。Windows受限环境如被ACL拒绝，仅需在已授权环境运行，不改ACL。脚本不创建manifest或approve审阅文件。重建改变冻结文件时须由验收阶段重新计算manifest与独立审批，旧审批不再有效。

统一合成值：必填record_id/summary；可选project_id/created_at/internal_note；显式别名ticket_no→record_id、issue_text→summary；viewer普通字段可读、internal_note不可读；editor/admin仍受项目及敏感字段授权约束；允许排序仅created_at/updated_at/record_id、asc/desc。以上是本合成文档约定，不向生产系统新增政策。

`case_annotations.jsonl` 记录120条历史所属节、条件和反例类型，不进入HistoricalCase本体或检索。`evaluation_annotations.jsonl` 记录测试路径覆盖和改写候选，不进入工单或索引。每节五历史依次成功、缺前提最终补齐、近似不适用、危险人工边界、相邻功能；逐历史与逐工单独立Agent复核见quality_review.md、corpus_reviews.jsonl和ticket_reviews.jsonl。

来源列表区分Case ID和Docs chunk ID。002/020/038/043的完整政策答案须来自Docs；043的046/106相关Case仅提供部分概念边界，不构成完整三角色答案；固定Case bootstrap始终发生。case_only表示案例足够，both表示条件互补，no_extra_tools表示bootstrap匹配时无需主动补查；这些覆盖注释不强制唯一工具路径。允许路径是bootstrap后的可能合理序列，动作评分不以走唯一序列为条件。query_rewrite_candidate是设计线索，并未执行模型或观察检索排名。

源标签保持 `pending_review`；只有独立审批记录可以让副本进入 `reviewed` 状态。数据生成脚本不会生成或刷新manifest、检索查询、报告或审批记录。

在仓库根目录执行以下命令，显式冻结候选快照并生成 test-only 查询与静态验证报告：

```powershell
uv run python scripts/validate_phase7.py --freeze
uv run python scripts/validate_phase7.py --check
```

`--check` 只读取并验证已有产物，不重建文件；若源数据、文档或规则改变，会报告快照过期。独立 Agent 完成 48 条审批并写入 `label_reviews.jsonl` 后，要求每条审批有效可运行：

```powershell
uv run python scripts/validate_phase7.py --check --require-reviewed
uv run python scripts/validate_phase7.py --check --export-reviewed-queries data/synthetic/phase7_v1/reviewed_case_retrieval_queries.jsonl
```

审批须使用 `review_method="independent_agent"`、带时区的 `reviewed_at`，且逐条绑定当前 `label_hash` 和 `snapshot_hash`。正式查询导出写到单独文件，不修改候选查询、manifest 或审批记录。相似文本报告只提供字符 n-gram 线索；近似匹配需要逐例语义判断，不能仅凭同主题判为重复；零自动候选也不能证明没有语义重复。

静态验证不访问网络、模型、embedding、Milvus 或日常数据库，也不证明 BM25、Dense、Hybrid 的检索效果或模型行为。48条全部test，没有开发集；这是公开生成的候选集，不称独立盲测或人类标注。不得用这48条test的排名或模型结果调参；后续接触测试必须披露。

冻结静态报告的review_status=candidate_unreviewed只描述源文件的候选属性；有效审批状态以--check --require-reviewed返回的reviewed为准。answer_available是Case/Docs联合答案可用性；无相关Case的查询只作Case开放集诊断，043虽有部分相关Case，也不能在Case检索评测中声称完整政策可由Case独立回答。

001/003/007/031/032/036/037/039/048等与旧同产品样本存在语义家族继承，详见quality_review.md；该公开合成测试集已接触，不是独立盲测或生产分布，不提供人类标注证据。7条query_rewrite_candidate只表示设计候选，未验证改写或检索效果。
