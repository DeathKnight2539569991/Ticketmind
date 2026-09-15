# M4 候选评测集

版本 `m4-reviewed-candidate-2`。用户委托Agent审阅，主Agent在真实Agent质量运行前核对并修订；不声称人类独立标注。源JSONL继续 `pending_review`，实际 `reviewed` 由独立内容hash审批记录加载。

- `evaluation_cases.jsonl`：39条新输入，18 validation / 21 test；14个问题组，同组不跨split。
- `evaluation_labels.jsonl`：对应39条候选标签，全部 `pending_review`。
- `development_labels_overlay.jsonl`：原v2的16条、M3缓存5条及诊断12条共33条开发标签补全；原文件保持不变。
- `build_candidates.py`：可重建候选JSONL的无网络脚本，保留初版定义和显式受托审阅修订块。仅在需要重建候选数据时运行；审批记录独立保存，不在此脚本中改为reviewed。

数据仅为同一合成产品、固定12条历史案例上的新输入候选。编写时参考了历史来源和开发错误类型，刻意加入实质前提变化，不能称作跨产品、外部、盲测或生产集。生成后若依据test结果改提示词、分词或检索参数，则该版本test应降为开发诊断，另建未参与调参的新输入。

第二版在查看BM25结果后按来源适用条件修订标签，没有改查询输入、分组或检索参数，也没有依据召回排名或模型输出选择正确标签。仅用于说明已排除原因/主题近似的来源不计相关命中。修订明细见逐例清单。

validation组：日期语义、CSV分隔符、CSV解码、游标上下文、客户端超时、代理超时。

test组：浏览器缓存、排序字段映射、仪表盘范围、权限核验、资金与安全、数据恢复、正式政策、安全核查。共8组；数据恢复2条、正式政策1条，其余组各3条。同一个问题组的匹配/缺信息/反例只落在一个split。风险类标签可跨组出现（如付款超时与账单核验），但不将同模板改写后拆到两边。

每个输入使用 `input`（含subject/body/channel/requester_role），检索文本统一由评测入口构建；旧数据仍用原request/query格式。所有样本均显式标为synthetic。标签动作取值与Agent协议一致：`propose_resolution`、`ask_clarification`、`escalate`。

规则与审核入口：[合成业务规则](../../../docs/synthetic-business-rules.md)、[逐例审阅清单](../../../docs/m4-label-review.md)。
