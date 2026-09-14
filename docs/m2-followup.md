# M2 补充后继续处理与重检索验收

当前状态：**GLM-5.2已完成真实重检索→适用证据建议→待审落库；用户批准具体编辑稿后，零新增模型调用完成审核应用，工单仍open。** 本轮约定的M2收尾已完成，原始草稿仍需人工编辑，不宣称单模型全场景可靠。累计21次请求尝试：理解3、首次向量3、重检索向量1、决策14（含1次模型名称NotFound失败）。下文保留各次实验当时的失败结论与原始报告，不覆盖历史证据。

## 一个场景覆盖两个剩余项

沿用已经批准的“API 超时”追问原文，以原报告的历史输出与证据快照重新建立临时工单并应用同一审核。通过真实 ASGI HTTP API 追加以下**合成客户补充**，不伪装成真实客户反馈：

> 补充准确接口信息：之前简称订单查询，实际是示例协作台的只读全年聚合报表查询。运行环境为 Python 3.12，没有使用本机开发代理。客户端等待上限是 2 秒；单日查询约 0.4 秒，全年查询总在 2 秒返回 E_TIMEOUT。同一网络下，维护人员已经做过只读诊断：允许等待 10 秒时，全年查询约 4 秒返回完整结果。我们只需要这次只读查询的处理建议。

工单应从 awaiting_customer/版本2 变为 open/版本3。新运行使用新 run/thread、同一工单的完整会话、已实际发布的追问与澄清轮数1，不拼造已问问题。

为了覆盖重检索，验收客户端在首次真实 Milvus 查询后，从**传给模型的候选列表**中去掉 006（若存在），其他候选保持原样；记录完整的实际返回与模型所见返回。后续查询完全透传真实 Milvus。这是**受控首检漏召回实验**，不评价自然首检效果，不修改、删除、重建集合或案例，也不注入假的模型 search_cases 决策。

预期模型自行发出基于已知事实的新 search_cases 查询，真实生成新查询向量、再查 Milvus、读到006后继续决策。若没有主动重检索，保留失败/未覆盖结果，不伪造、不自动反复调用。最终预期是有006证据的只读查询建议；10秒只能依据这条合成补充中已完成的诊断，不推广到支付或其他写入请求，不宣称已操作或已解决。

## 新增预算

| 类别 | 新增上限 | 原因 |
| --- | ---: | --- |
| 理解 | 1 | 新快照含客户补充与实际追问；旧理解不能复用 |
| 首次 Embedding | 1 | 新完整会话的检索输入不同 |
| 重检索 Embedding | 1 | 模型选择的新查询，先查精确指纹缓存 |
| 决策 | 3 | 首次决策及工具后的继续决策，仍受8步上限约束 |
| 合计 | **6** | 理解1、Embedding合计2、决策3；不是必须花完 |

如果只经历“决策选搜索→再决策给建议”，实际为5次。失败、超时、无效响应也计次；不自动重试。使用原 attempts.json，不清空、不换台账。获批后累计类别上限设为理解3、首次向量3、重检索向量1、决策6，即原实际7次加本次最多6次。旧追问回放独立强制为0新增额度，即使新场景已获授权也不能用其额度重算旧提案。

## 运行

```powershell
# 显示固定输入及预算，不访问服务
uv run --no-sync python scripts/check_m2_followup.py

# 真实数据库/只读Milvus + 旧缓存；构造新快照并检查新指纹，新增模型0
uv run --no-sync python scripts/check_m2_followup.py --prepare

# 仅在用户批准本场景新增≤6次调用后执行
uv run --no-sync python scripts/check_m2_followup.py --execute --allow-followup-budget
```

报告为 data/cache/m2/acceptance/reports/followup-*.json，含客户补充后工单、原审核、新快照、真实与可见检索结果、逐轮原始决策、新调用记录及最终提案。保留新理解/向量/决策缓存。默认新提案停在 waiting_review，版本仍3、消息仍3，不自动发布新建议或关闭；获得具体审核后用 `--review` 加绑定 proposal_hash 的审核文件，四类新调用额度保持0回放应用。

本次新增两个真实 PostgreSQL/检查点集成测试（模型及Milvus为替身），与已有相关测试合计10项通过。首次准备已验证客户补充写入/状态/版本，旧三类缓存命中、新理解与向量缓存均缺失；准备阶段新增模型0次。后续执行情况见下文。

## 首次失败、修复与复验

原失败报告：`data/cache/m2/acceptance/reports/followup-execute-b377152de2704f40bd27be89cceefa47.json`。真实 Milvus 首检为006/007/001；受控过滤后模型看见007/001。模型发出 `get_case_detail(007)`，随后却声称007支持调整超时。实际上007明确说明是代理配置问题，与客户端等待上限无关。模型还从客户测得的10秒设置扩展出未验证的5–10秒范围。此提案结构合法但依据错误，验收失败并保留原文，未发布。

新协议 `m2-evidence-quotes-v1`：新的解决提案必须提供 evidence_quotes，逐一对应引用ID，内容须是该实际来源中的连续原文；提示词强调排除条件、证据不适用时重检索、不外推参数和不保证成功。旧已持久化提案仍可读取/恢复，新增真实决策按新协议验证。原文存在不证明语义适用，仍须人工审核。相关44项定向测试通过（含5项真实数据库测试，模型/Milvus为替身）。

协议改变后不强行使用旧决策指纹。初始已批准的追问从原报告按原提案摘要回放，明确标记为**历史模型输出与证据快照**，不说成当前新协议的缓存命中；不重算旧提案。客户补充后的理解与首次向量输入未变，复用刚保存的精确缓存。

修复复验的**新增上限4次：重检索 Embedding≤1、决策≤3，理解/首次向量均0**。预计“决策→重检索→决策”用3次，工具详情多一轮时最多4次。此前首次实验未使用的额度不自动解释为修改提示词后可重试，复验须明确授权；同一台账累计上限为理解3、首次向量3、重检索向量1、决策8。失败不自动再试。

```powershell
# 当前协议准备：历史已审核追问快照回放，新理解/首次向量应匹配；新增0
uv run --no-sync python scripts/check_m2_followup.py --prepare

# 单独取得修复后复验≤4次授权后使用
uv run --no-sync python scripts/check_m2_followup.py --execute --allow-recheck-budget
```

## 复验实际结果与零调用防护验证

用户已授权修复后最多4次；实际新增**2次决策**，理解与首次向量精确缓存命中，未生成重检索向量。报告：`data/cache/m2/acceptance/reports/followup-execute-67973210fc734714bfbcdc8c64c5e273.json`。模型再次选择读取007详情，随后询问“是否同意将客户端请求超时时间调整为大于5秒（例如10秒）”，把操作建议包装成追问，仍未选择 `search_cases`。因此复验失败，不继续消耗剩余额度追求通过。

追问规则原来漏掉“调整”等操作词，现补充调整/调高/调低/调大/调小及对应英文负例。定向34项逻辑/替身测试通过。规则仍是启发式；不能证明所有追问安全，也不能保证引用原文与当前问题语义相关。

随后用同一输入、未改指纹的理解/首次向量/两轮决策缓存，强制新增额度为0，经过真实 ASGI HTTP、PostgreSQL/checkpointer、只读 Milvus 回放。报告：`data/cache/m2/acceptance/reports/followup-execute-6eee801f93964efda9dca5d4501ff2c1.json`。运行按预期成为 `failed / agent_execution_failed`（decision 阶段），proposal、review、published_message_id 均为空；工单保持 open/版本3/消息3。新增调用0，台账仍13次。这证明已保存的错误输出被当前规则拦截，不是新真实模型成功链路。

```powershell
# 零额度检查：新协议决策缓存缺失时会明确失败，绝不自动补调
# 前述错误追问的缓存防护回放已在 v1 协议下完成，保留原报告
uv run --no-sync python scripts/check_m2_followup.py --execute
```

客户补充、新快照、新线程、澄清轮数及模型选取详情工具后的继续决策已有真实证据；**自主重检索及取得适用案例后的新建议未通过本次验收**。旧三条审核演示保留，但属于证据原文协议加入前。下一次质量复验须单独固定输入/协议/预算并确认授权，当前不追加调用、不发布失败提案、不关闭工单。

## 动作边界复验（已执行，协议失败）

当前协议为 `m2-action-boundaries-v2`，保留证据原文约束，明确区分缺客户事实与缺案例证据：只追问尚未回答且影响判断的事实；客户信息已足够但案例不适用时，使用剩余检索额度或转人工；“是否同意操作”由人工审核承担。没有指定查询内容、来源ID或强制工具调用，是否检索仍由模型选择。

固定输入、首检漏召回条件、模型配置和通过标准均与前两次相同。真实准备确认理解和首次向量精确缓存命中，新提示词导致旧决策指纹不匹配，保留旧文件。用户已确认**最多4次，决策≤3、重检索Embedding≤1，理解/首次Embedding均0**；计划通常为决策→重检索向量→决策共3次，若多读一次详情则最多4次。原13次台账不清空，类别累计上限为3/3/1/10。一次运行后不自动重试；失败、超时与无效输出计次。

验收脚本现会在断言前导出被规则拒绝或不合法JSON的原始响应、协议版本、动作匹配与重检索结果。15项定向测试通过（11逻辑/替身、4真实PostgreSQL；模型/Milvus均为替身），覆盖失败运行不发布且报告保留原文。本次准备新增真实模型0次，累计仍13次。

```powershell
# 仅在用户对本次动作边界复验明确授权≤4次后执行；默认停在待审
uv run --no-sync python scripts/check_m2_followup.py --execute --allow-boundary-budget
```

成功判定需分别核对：真实模型主动搜索和真实新向量/Milvus证据；原文是否适用、是否仍重复追问或外推参数；新提案是否仅保存待审。进入 waiting_review 不等于内容可靠或审核通过。新增提案不沿用旧三例审核授权。

实际新增**2次决策**，total tokens 分别3522、4087；理解与首次向量缓存命中，重检索Embedding0，累计15次。报告：`data/cache/m2/acceptance/reports/followup-execute-6dc96bd1a6cf435391d0150d5b43dc2b.json`。第一轮为合法 `get_case_detail(007)`；第二轮承认007/001不适用，返回 reason/query/missing_evidence，但**没有 next_step**。这是重检索意图的原文证据，不是合法工具请求，也没有执行重检索或生成最终建议。运行 failed，proposal/review/published_message_id 均为空，工单仍 open/版本3/消息3。

验收适配器新增独立校验诊断，区分响应完成、结构和提案规则；不改原始输出、不补字段、不改提示词指纹。台账的 succeeded 表示请求返回，不代表结构或业务验收成功。17项定向测试通过（12逻辑/替身＋5真实PostgreSQL；模型/Milvus替身），覆盖缺少动作字段时拒绝执行且不发布。

同一两份真实决策缓存离线回放，新增调用0、工具执行0；第一轮结构通过，第二轮明确 `schema / union_tag_not_found`，诊断指向 next_step。独立报告 `data/cache/m2/acceptance/reports/followup-boundary-diagnostic-39809cbebdb741c587faedf41f478109.json`，原失败报告不覆盖。本次授权的一次运行已结束，不用剩余额度自动重试。剩余验收是**合法重检索请求→真实新向量/Milvus→适用证据支持的新建议**；当前不能宣称该路径通过。

## qwen3.8-27b 同条件对比

用户要求直接换用 qwen3.8-27b 试验后，只替换本次验收的决策模型。理解仍为 qwen3.7-flash 的精确缓存，Embedding仍为 text-embedding-v4；模型身份分别记录。输入、提示词、JSON模式、temperature=0.2、max_tokens=1600、enable_thinking=false、首检漏召回条件均保持一致；未更改 .env 的全局默认模型。

沿用单次最多4次边界：决策≤3、重检索向量≤1，理解/首次向量新增0，原15次台账保留，类别累计上限3/3/1/12。实际只调用**1次决策**（prompt3421、completion240、total3661，finish_reason=stop），不是截断；累计16次。报告 `data/cache/m2/acceptance/reports/followup-execute-f1f420c8a55e45b3bc3e6a07cbaf0919.json`。

原始输出是单元素数组，元素含 next_step=search_cases、reason、query、missing_evidence。它直接识别007/001不适用并按客户事实提出新查询，没有先读007详情、重复追问或漏动作字段。但当前协议要求单个对象，结构校验为 `schema / dict_type`；未自动拆数组，未发出重检索向量请求，未产生最终建议。这个样本的动作选择更贴合要求，不能据此断言总体模型能力更强或整个链路已通过。

报告的 raw_decision_responses 保留数组原文；raw_decisions 只收录可解析的对象，空列表不代表供应商没有响应。运行 failed，proposal/review/published_message_id 为空，工单 open/版本3/消息3。6项定向集成检查通过（真实PostgreSQL，模型/Milvus为替身；含模型身份分离、数组拒绝且不执行工具）。无自动重试、发布、关闭、提交或部署。

```powershell
# 本次已执行的授权命令；再次试验需核对授权，默认不自动重试
uv run --no-sync python scripts/check_m2_followup.py --execute --decision-model qwen3.8-27b --allow-model-comparison-budget
```

## qwen3.8-max-0902 同条件对比

按用户要求只替换决策模型为 qwen3.8-max-0902；输入、提示词、生成参数与检索条件保持一致，理解/首次向量精确缓存。单次最多决策3＋重检索向量1，保留原16次台账，类别累计上限3/3/1/13。实际仅新增**1次决策**（prompt3421、completion207、total3628），累计17次；没有重检索Embedding。报告 `data/cache/m2/acceptance/reports/followup-execute-f294f5153e1742039fccdcb40fe46e23.json`。

本次输出为合法单对象，next_step=escalate，结构和当前规则通过；真实ASGI HTTP/PostgreSQL/checkpointer将提案保存为 waiting_review。模型正确指出007要求代理条件、001是日期边界，均不适用。但发送给模型的状态还有1轮检索额度、5个步骤，模型未先重检索就转人工，未达到本实验的预期动作；不能将这次格式正确或待审落库称为重检索通过。

原始草稿还声称“已将您的工单升级至技术支持团队进一步评估”，并承诺技术人员给出后续建议；当前只是未审核提案，系统也未接入外部团队转交，应由人工修改这些表述。没有将其发布或沿用旧审核授权。运行当时工单open/版本3/消息3，review与published_message_id为空；临时schema随后清理，完整提案及调用证据保存在报告中。开发库仍1工单/1消息/0运行，未改全局默认模型。

同一 v2 协议的单例观察（不作为准确率或总体模型排名）：

| 决策模型 | 实际新增决策 | 原始输出与执行结果 |
| --- | ---: | --- |
| qwen3.7-flash | 2 | 先读007，随后有重检索意图但漏next_step，failed |
| qwen3.8-27b | 1 | 首轮提出检索且字段齐全，数组形状不符，failed |
| qwen3.8-max-0902 | 1 | 格式正确，直接转人工，waiting_review；未重检索 |

代码审核/事务/恢复/工具执行已有逻辑和真实数据库验证；本次也新增合法模型输出保存待审的真实证据。剩余问题同时涉及模型协议遵守、工具选择与提案措辞，不能归结为“代码因格式问题完全无法验证”，也不能认为换更大模型就已解决。未机械重跑既有测试、未追加付费重试。

```powershell
# 本次已执行的单次授权命令；不代表无限重复授权
uv run --no-sync python scripts/check_m2_followup.py --execute --decision-model qwen3.8-max-0902 --allow-max-comparison-budget
```

## GLM-5.2 实际通过与人工审核

用户指定GLM5.2后，初次以glm5.2请求返回NotFoundError，无usage或模型输出。只读模型列表确认正式名称glm-5.2，在同一授权上限内修正名称；没有切换其他供应商或扩充预算。失败请求报告 `followup-execute-dd15e8a32c2a4bfea5d846756729816c.json` 保留并计入尝试数。

正式名称请求实际新增**2次决策＋1次重检索Embedding**，连同名称失败共4次；累计台账21次（20次有返回、1次NotFound失败）。理解/首次向量精确缓存命中，未改指纹。第一轮system/user提示词与Max对比逐字一致，生成配置不变，仅决策模型改变。GLM第一轮合法search_cases，查询只使用客户事实；新向量经真实Milvus召回006/007/001，006排第一；第二轮提出引用006连续原文的propose_resolution。真实ASGI HTTP/PostgreSQL/checkpointer保存为waiting_review。

调用证据：两轮决策total tokens为3530、5435，重检索Embedding为30；精确指纹、供应商响应ID与原始文本均在 `data/cache/m2/acceptance/`。报告：`followup-execute-c73b730d88bc4f399e538a0ab0b5738d.json`。这是受控首检漏召回实验的真实模型/向量/检索证据，理解与首次向量来自历史缓存，不是一次所有阶段全新调用，也不是自然召回率评测。

原始提案正确引用适用006并限定本次只读查询，但“并非接口本身异常”过度断言，且混入内部审核说明。用户已批准[完整编辑稿](m2-followup-review.md)，删除这些表述，保留本次已验证10秒设置及受控验证。

审核阶段强制四类新调用额度均0；五份精确缓存命中（理解1、首次向量1、重检索向量1、决策2），Milvus真实只读。审核edit后运行completed，工单open/版本4/消息4；原提案与final_reply保持原文，review.edited_reply及发布消息等于批准稿，重复审核200且不重复追加。没有关闭或对外发送。报告 `followup-execute-3d38b3f07b534411b5f43d553a992a7b.json`。

所有临时schema已清理，开发库仍迁移9c42d71ab203、工单1/消息1/运行0，原Milvus集合/缓存保留；无提交、推送、部署或M3扩展。三类首次决策与审核是早期协议的历史验证，GLM验证了当前协议的补充后重检索解决路径；不宣称GLM已逐项重测三条路径，不抹去其他模型的失败结果。

```powershell
# 已执行的GLM授权计算；正式名称有连字符，失败名称请求仍计入同一台账
uv run --no-sync python scripts/check_m2_followup.py --execute --decision-model glm-5.2 --allow-glm-comparison-budget

# 用户已批准该具体编辑稿；精确缓存审核回放，新增调用额度强制为0
uv run --no-sync python scripts/check_m2_followup.py --execute --decision-model glm-5.2 --review data/cache/m2/acceptance/glm-followup-review-proposed.json
```
