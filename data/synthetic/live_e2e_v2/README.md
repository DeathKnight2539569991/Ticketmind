# Live E2E v2：纠正历史语料后再验收

此目录为**新的、独立的合成测试夹具**，不改 `live_e2e_v1`、旧 PostgreSQL 数据集、旧 Milvus 索引、旧工单及其审核记录。详细逐条检查见 [QUALITY_REVIEW.md](QUALITY_REVIEW.md)。

文件用途与 v1 相同：`historical_cases.jsonl` 为 10 条供 seed 的合成历史案例；`test_tickets.jsonl` 为 21 条新工单输入；`review_checks.jsonl` 为隔离于 Agent/Judge 的人类检查草案；`customer_followups.jsonl` 是多轮客户消息；`validate_suite.py` 只做本地静态结构及本次修订的一致性检查。

**v1 的 TEST-01（北京时间 23:00 / 同日 UTC 15:00）与 v2 的 TEST-01（北京时间 00:30 / 前一 UTC 日 16:30）不是同一个输入。** 不能把两者的模型输出直接算成同题前后对照；旧案例 001 的缺陷及旧审核记录作为失败证据保留。

## 1. 零模型调用离线检查

在 Windows PowerShell 项目根目录：

```powershell
git pull --ff-only
uv run --no-sync python data/synthetic/live_e2e_v2/validate_suite.py
```

如果你目前还在运行旧数据集的工作台，先不要重启或切换。下列步骤会建立新的合成知识数据集；不会自动修改既有工单或其绑定的 corpus_version。

## 2. 建立新的合成数据集（有数据库写入，无模型请求）

```powershell
$env:TICKETMIND_CORPUS_PATH = (Resolve-Path .\data\synthetic\live_e2e_v2\historical_cases.jsonl).Path
$seedOutput = uv run --no-sync python scripts/knowledge.py seed
$seedOutput
$dataset = ($seedOutput | Select-Object -Last 1 | ConvertFrom-Json).dataset_version
$dataset
```

`$dataset` 必须是新版本，不要继续使用旧的 `synthetic-v2-638d9040...`。这一步**只导入 PostgreSQL**，还不能直接用于 Milvus 检索。

## 3. 建索引（可能产生真实文档 Embedding 费用）

同一业务空间、同一 Embedding 模型 `text-embedding-v4` 下，历史案例 002–010 的完整嵌入文本与旧版相同，旧有的九条精确向量缓存应可复用；001 的文本已改，需要一条新的文档 Embedding。不同业务空间、丢失缓存或其他配置变化则可能需要更多调用，**不要强行重置额度/台账**。

若愿意为本次新的 001 向量授权至多 **1 次新的文档 Embedding 请求尝试**，使用全新的 v2 台账路径：

```powershell
uv run --no-sync python scripts/knowledge.py reconcile --dataset $dataset --embedding-budget 1 --ledger data/cache/live_e2e_v2/embedding_attempts.json
```

查看输出：10 条均为 `active`、`index_error=null` 才进入下一步。若显示 `embedding_cache_missing` 或 `knowledge_index_failed`，不要删除台账或盲目重复运行，先核对原因和缓存；失败请求仍可能计费。仅重复 `seed` 不会修复索引。

## 4. 使用新知识数据集启动真实链路

在停止旧的 API/工作台后，在**同一个 PowerShell 会话**中执行：

```powershell
$env:TICKETMIND_KNOWLEDGE_DATASET = $dataset
$env:TICKETMIND_RETRIEVAL_MODE = "bm25"
uv run --no-sync python scripts/start_local.py --skip-infra
```

不要使用 `--demo`。进入工作台确认是 `live`；创建**新的 TEST-01 工单**，只录入 `test_tickets.jsonl` 的 `input` 字段，不要将审核草案输入模型。核对新运行的 `corpus_version` 是本次 `$dataset`，以及 evidence 001 的 UTC 时间戳、日桶边界是否正确。旧工单依然绑定旧版来源，不会因切换设置而追溯改写。

此次工作只修订语料，不改已有 Agent 决策/引用校验/Judge 逻辑，也未运行真实 PG/Milvus/模型链路。 
