# 项目资料

本目录保留可维护的项目资料、操作指南和规则契约。测试输出、模型响应、日志、历史验收及过程记录统一归档到项目外层 `D:/AnalyzeAgent/log/`，不再写入本目录。

| 资料 | 用途 |
| --- | --- |
| [架构与接口](architecture.md) | Agent 流程、业务状态、审核、持久化和恢复 |
| [演示指南](demo-guide.md) | 启动和工作台操作 |
| [知识库操作](knowledge-writeback.md) | 历史语料、知识发布与索引维护；历史行为已标注 |
| [交付说明](internship-handoff.md) | 展示范围和使用入口 |
| [面试提纲](interview-guide.md) | 项目讲解和设计取舍 |
| [来源与贡献](sources-and-contributions.md) | 基线、复用来源和实现范围 |
| [合成业务规则](synthetic-business-rules.md) | 示例场景的判定边界，不是真实产品政策 |
| [阶段 7 数据契约](phase7-data-contract.md) | synthetic Docs、Cases、测试输入与标签契约 |

合成产品 Markdown 保存在 [数据集目录](../data/synthetic/phase7_v1/docs/)，与工程资料分开；该目录只用于文档导入。阶段 7 数据的结构、标签、生成和审批说明见[数据集 README](../data/synthetic/phase7_v1/README.md)。

历史报告和原始证据见[外层归档索引](../../log/app-docs/INDEX.md)。这些记录保留当时模型、版本和结果，不能当作当前配置或新调用授权。外层 log 是本机归档，不随项目仓库自动分发。

任何真实模型或 Embedding 调用前，先向用户确认本轮模型、接口、必要配置和额度；当前使用记录为 `qwen3.8-flash`，不自动构成调用授权。
