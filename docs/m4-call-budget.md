# M4 调用方案与 A 档执行记录

> 2026-09-24 兼容说明：下文 A/B 档命令和结果是旧 `qwen3.7-flash` / `glm-5.2`、旧协议的历史记录，不构成当前模型调用授权，也不能直接作为当前双模型质量验收。现行 `scripts/evaluate_m4.py` 的 Agent 默认 Decision=`qwen3.8-flash`、Judge=`deepseek-v4.1-flash`；两者由独立缓存、请求指纹和累计上限控制。当前 Agent 执行必须显式列出 `--case`，并按已获批准范围分别传入 `--decision-ceiling`、`--judge-ceiling`（以及需要新向量时的 Embedding 上限）。所有新上限默认 0；`--execute` 与 CLI 上限本身都不授予付费许可。Judge 原始响应存放 `data/cache/m4/judge/`，新 `attempts.json` 追加 `judge` 类别；原旧类别与旧缓存保持可读，缺 Judge 上限的旧脚本不能发送 Judge 请求。旧历史结果不重算或改写。

既有额度已用完后，用户明确授权A档（最多81次），并委托Agent审查标签。A档已执行完成：**65次请求尝试，全部成功返回**，固定向量51、理解6、决策7、重检索向量1。结果见[m4-agent-results.json](m4-agent-results.json)。B档尚未授权；未使用的A额度不用于新样本或提示词重测。

实际usage：固定向量2724 tokens、重检索向量25 tokens；理解prompt 2410 / completion 505；决策prompt 30967 / completion 1925。价格未知，不换算金额；没有业务审核、发布或关闭。

## 输入和模型

- 固定查询向量：新集 `SYN-EVAL-M4-001`—`039` 共39条，加原诊断 `SYN-RET-M3-001`—`012` 共12条，总51条。完整输入见 [标签清单](m4-label-review.md)。旧5条缓存向量无需重调，12条历史文档向量继续复用。
- 理解保持 `qwen3.7-flash`，决策单独使用 `glm-5.2`；不修改全局配置。Embedding 为 `text-embedding-v4`、1024维。
- 固定 Agent 配置：Hybrid，top_k=5、candidate_k=20、RRF k=60；当前 `m3-retrieval-evidence-v1` 协议，每单至多理解1次、重检索向量1次、决策3次。只生成待审核提案；不批准、不发布、不关闭。
- 理解输出上限512 tokens，决策输出上限1600 tokens/次；这只是请求参数，不是已发生用量或金额预算。

## 可选的明确范围

| 范围 | 首次向量累计上限 | 理解累计上限 | 重检索向量累计上限 | 决策累计上限 | 请求尝试累计上限 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A：51条检索向量 + 6条 validation Agent 试跑 | 51 | 6 | 6 | 18 | **81** |
| B：51条检索向量 + 完整39条 Agent（包含A，非另外相加） | 51 | 39 | 39 | 117 | **246** |

A的6个固定Agent样本：004分隔符建议、006文件内容涉及写入的反例、011新游标仍失败、013受控只读查询耗时、015支付写入超时、018组织代理条件。完整原文与候选动作以清单为准。A仅是validation试跑，不能据此宣称独立test的Agent质量完成；B包含全部39条。可以先批准A，查看原始失败后再决定剩余范围。

以上是请求**尝试数上限**，失败、NotFound和超时仍计次。直接完成决策时只消耗实际所需调用；高风险代码直接转人工时可能没有模型决策。不能把代码兜底当模型决策成功。

价格尚未核实，不提供金额估计；实际可获得的 token usage、请求ID、耗时和失败记录随报告保存。缺失 usage 显式保留 null。若需要金额上限，需先核对当前供应商报价并另行设置，不能将上述尝试数换算为未经核实的价格。

## 执行约束与命令

所有新缓存和累计台账固定在被忽略的 `data/cache/m4/`；成功后立即缓存，失败不自动重试，不清空台账。相同指纹没有缓存且已有尝试时停止该请求。两阶段共用首次向量缓存，Agent首检不会重复请求同一查询向量。

A档原始执行命令如下。现在应保留既有台账与缓存，禁止为重新获得额度而删除它们；在新机器复现付费阶段需要重新授权：

```powershell
uv run --no-sync python scripts/evaluate_m4.py --stage vectors --execute --include-m3-diagnostics --initial-embedding-ceiling 51
uv run --no-sync python scripts/evaluate_m4.py --stage retrieval --include-m3-diagnostics
uv run --no-sync python scripts/evaluate_m4.py --stage agent --execute --case SYN-EVAL-M4-004 --case SYN-EVAL-M4-006 --case SYN-EVAL-M4-011 --case SYN-EVAL-M4-013 --case SYN-EVAL-M4-015 --case SYN-EVAL-M4-018 --initial-embedding-ceiling 51 --understanding-ceiling 6 --research-embedding-ceiling 6 --decision-ceiling 18
```

B授权后使用同一入口，显式传入001—039全部case，并将三个上限改为39/39/117；首次向量仍51，绝非51+51。不因失败或提示词调整暗中扩额度。

## 标签审核独立于调用授权

用户明确回复“请你自行查看标签，你来判断是否可以”，因此由Agent核对全部72条并保留审查方法为`user_delegated_agent`。M4-030保留转人工：特定项目原因依赖当前工具不可访问的管理记录，不推广为所有权限概念咨询都转人工。完整修订见[清单](m4-label-review.md)。这不是人类独立标注。

`docs/m4-preflight.json`保留授权前快照；当前预检为`docs/m4-reviewed-preflight.json`。独立审查文件在`data/synthetic/m4/{label_reviews,development_label_reviews}.jsonl`，绑定输入、标签和语料版本；草案保持pending，加载有效记录后为reviewed。调用授权不自动批准标签，标签批准也不自动授权付费。

必要追问覆盖与禁止结论通过单独的受托Agent语义审查记录评判，绑定标签hash和原始预测hash；动作和引用另作确定性计数，未让被评测模型自评分。仍缺独立人工复核，不能据此宣称已有人类标注质量保证。人工业务审核/修改与模型原始质量分开，不自动发布真实输出。
