# 收尾验收记录（2026-09-28）

结论：本次已确认的本地实习展示范围完成，工程回归通过。能够展示工单创建、Agent 提案、人工审核、客户补充、明确关闭和知识沉淀，并有可核对的历史真实模型评测。此结论不等于模型所有路径均已实测通过或生产上线验收。

## 本轮交付

以 Git `69630c9` 为基线，补齐[交付说明](internship-handoff.md)、[五分钟演示](demo-guide.md)、[RAG 评估](rag-evaluation.md)和[面试材料](interview-guide.md)，更新 README 与历史文档指引。配置示例使用百炼 qwen3.8-flash Decision、deepseek-v4.1-flash Judge、BM25；已有 `.env` 保留原样。

新增离线证据导出脚本 `scripts/export_review_evidence.py`，将既有 40 题、80 次双模型运行及近期真实路径摘要整理成[仓库内证据](evaluation/evidence-summary.json)，保留原文件哈希。新增 `scripts/verify_project.py` 保存带时间与 UUID 的测试记录，便于复现。本轮未修改 Agent loop、业务代码、数据库迁移或依赖。

## 实际执行与结果

| 检查 | 结果与范围 |
|---|---|
| 演示启动预检 | `scripts/start_local.py --demo --check` 通过，PostgreSQL 可连接，默认演示端口可绑定 |
| 完整 pytest 回归 | **286 passed，0 failed / error / skipped**，耗时 **103.746 秒** |
| 真实 PostgreSQL | 启用集成测试，在随机 UUID schema 中执行迁移、业务/API、检查点与状态校验 |
| 真实 Milvus | 启用集成测试，在受保护的独立 UUID 集合中验证知识发布、三种检索、修复与停用；冻结历史索引仅只读对照 |
| UI 集成 | Streamlit AppTest → 本机 HTTP → 真实隔离 PostgreSQL，包含审核、补问、失败及知识操作 |
| 模型调用 | Decision / Judge / Embedding 均无新增付费调用；使用确定性替身及已有缓存 |
| 历史证据整理 | 离线导出成功；80 条运行、40 条逐题双模型判定；原始归档不被修改 |

命令从仓库根目录执行。本机实际使用 `.venv\Scripts\python.exe` 运行以下脚本，已配置环境下也可使用 uv：

```powershell
uv run --no-sync python scripts/start_local.py --demo --check
uv run --no-sync python scripts/verify_project.py --db --milvus
```

完整回归结束于北京时间 2026-09-28 09:50:24。[机器可读结果](evaluation/verification-2026-09-28.json)保留计数、时间与原始日志 SHA-256；原始 JUnit、日志与摘要在本机 `data/cache/verification/20260928T014837Z-5a349329bb8e44c09a38c1039efe4884/`，该缓存目录不随 Git 提交。零付费属性来自测试中注入的模型/向量替身与缓存，不是对真实模型计费系统的在线统计。

出现两条依赖弃用警告：DashScope Assistants、langchain-community。没有测试失败；本轮不引入依赖升级，后续升级时应重新验证调用适配器。

## 结论边界

- UI 验证采用 AppTest 与真实 HTTP/PG，没有额外录制浏览器演示视频或宣称完成浏览器视觉验收。
- 本轮没有新增真实模型回归。历史 40 题对比与 9 月 26 日路径测试配置不同，分别列于[RAG 评估](rag-evaluation.md)，不能合并成当前准确率。
- 最新真实模型仍未自然触发再次检索、详情读取、Judge 拒绝修正；037 的两轮流程包含测试快照恢复，不是连续端到端运行。
- 本地单实例、合成数据与人工审核是交付边界；不包含真实客服渠道、生产并发或线上运维验收。

演示时先运行隔离模式，再讲解真实评测中的成功与失败案例；简历指标只引用已记录的实验条件与结果。
