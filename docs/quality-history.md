# 检验明细追溯与原请求核对

本轮新增 3 个只读质量工具，质量工具总数从 33 增至 36。工具已注册到 MCP、工具目录、动态加载和 Agent 路由，未手动重启运行中的服务。

后续批维护也复用候选查询与原请求核对：`UpdateSamplingLotHistory`有明确`SamplingLotId`，按汇总保存的`StoredComments`匹配原评论，比较数量、频率及冲销状态；保留选项和任务重算未核实，最多partial_match，不解锁。当前工具总数见[主目录](quality-mcp-tools.md)，参数见[批维护](quality-lot-maintenance.md)。

| 工具 | 必需参数 | 用途 |
| --- | --- | --- |
| `get_inspection_result_history` | `summary_history_id` | 展开一个检验汇总历史的候选样本/检验点关联，返回原值、限值、通过/拒收、历史身份和冲销状态 |
| `find_quality_execution_candidates` | `plan_id`, `plan_digest` | 查找已尝试、仍为 unknown/in_flight 的检验、批采样或整批 OK 执行候选；可用 offset/limit 分页 |
| `reconcile_quality_execution` | `plan_id`, `plan_digest`, `candidate_txn_id` | 核对明确候选事务、原请求与完整有界历史；返回字段匹配、差异或未完成情况，供人工审查 |

## 调用顺序

已有容器名称时，先 `get_container_inspection_history(container_name=...)`；已有抽样批时，先 `get_sampling_lot_history(lot_name=...)`。从返回行取得 `CollectSamplingDataHistoryId`，作为 `summary_history_id` 调用明细工具。不能把 `TxnId`、`HistoryId` 或 `HistoryMainlineId` 当作汇总历史 ID。

执行结果未知时，从原计划取得 `plan_id` 和 `plan_digest`，先查候选，再用候选实际返回的 `TxnId` 调用核对工具。多个候选不能默选为成功；候选分页没有唯一性含义。同一事务含多个符合条件的汇总也会返回 `ambiguous`，不会拼成一次请求。

```json
{"tool":"find_quality_execution_candidates","arguments":{"plan_id":"原计划ID","plan_digest":"原计划摘要","offset":0,"limit":20}}
```

```json
{"tool":"reconcile_quality_execution","arguments":{"plan_id":"原计划ID","plan_digest":"原计划摘要","candidate_txn_id":"从候选结果取得的16位TxnId"}}
```

候选查找校验原计划摘要、部署环境、单次执行尝试记录和状态，精确比较原请求完整评论（包含关联标记及用户备注）。采样还约束历史容器、固定版本测试以及历史抽样批 ID，不根据容器当前归属猜历史。

## 关联和比较的边界

现场 EDM 明确 `HistoryId` 为参与同一事务的容器共享标识，不能当作逐层父键。历史子实体虽然出现在 Query EDM，但探测的集合 GET 返回 405、按 ID GET 返回 404，不能据此生成可用读取工具。

当前改用部署表结构对应的固定 SQL：样本的 `HistoryDetailsId` 候选关联汇总主键，点的 `HistoryDetailsId` 候选关联样本主键；每层同时约束 `TxnId` 和共享 `HistoryId`。字段、表名和排序固定，输入仅接受严格 ID 或转义后的原评论。现场 5 条新查询均已返回 HTTP 200，但没有非空检验历史可验收这组父键映射，所以始终返回 `ownership_linkage_verified=false`，分组只作候选证据。

最多展开 50 个样本、100 个点。查询额外读取一条检测超界，重复、跨父级或跨事务记录、超界和读取期间变化都失败；没有样本明细时返回 incomplete，不能把空记录说成完整。结果超过统一输出上限也返回错误。重复读取不等于数据库事务快照，不能排除并发更改后恢复的情况。

测量检验比较测试/点 ID、数据类型、字面数值、上下限、通过/拒收及拒收数。数字使用 Decimal 比较，不求值表达式。历史缺少已核实的样本序号，按无序样本多重集合核对，保留重复样本的数量，不能按历史 ID 排序伪造“第几个样本”。

计数历史没有已核实的本次采样增量字段；累计数不能当作增量，`EnteredQty` 与拒收原因归属仍待核实。因此可比较字段一致时也只返回 `partial_match`。整批执行历史尚没有已核实的目标抽样批键，不能把通用 `BatchId` 或历史名称当成该批的最终因果证据。

主历史冲销状态使用 EDM 明确映射：1 Active，2 Deleted（原事务已被冲销），3 Reversal（补偿事务）。只有 Active 且事务类型符合原服务才可报告可比较字段匹配。`payload_match` 仅表示已比较的测量字段相同，仍有父键、样本顺序、原请求选项和最终事务效果等未核实项。

所有结果固定 `verified_success=false`、`request_correlation_verified=false`、`state_changed=false`、`retry_allowed=false`。不保存成功结论，不改 journal 或占用，不重放原请求；人工最终证据仍通过原有 `review_quality_transaction` 确认流程处理。

## 多轮问法验收

| 用户问法 | 正确调用与参数来源 | 结果要求 |
| --- | --- | --- |
| “查看容器 C 的检验历史” → “展开这一条的逐点结果” | 容器历史 → 将所选行的 CollectSamplingDataHistoryId 传给明细工具 | 原始点值、限值及拒收如实展示，父键关联注明待现场验收 |
| “这次批采样提交超时了” → “查这个计划的候选事务” → “核对事务 X” | 用原 plan_id/digest 查候选 → 将实际返回的 X 传给核对工具 | 未尝试计划、摘要错误或环境不同先拒绝；保留 unknown |
| “两份相同样本是否都记上了” | 核对完整样本多重集合 | 一个相同样本不能代替两个，历史重复行不能合并后算匹配 |
| “已冲销的这条算成功吗” | 查冲销状态后核对 | Deleted/Reversal 不得报告 payload_match |
| “累计采样 500，说明这次采了 5 吗” | 核对计数记录及未核实项 | 不用累计数证明本次增量，最多 partial_match |
| “整批历史有同名记录，可以自动解锁吗” | 候选查找 → 核对整批历史 | 目标批关联未核实，最多 partial_match；保持占用 |

## 证据

本轮新增 71 项历史核对测试；全量回归 1572 项测试、2 项子测试通过，其中质量相关 366 项，父 pytest 进程的离线 HTTPX 真实网络尝试为 0。真实 FastMCP Client 验证了候选查询 → 明细读取 → 原请求核对，以及额外参数、宽松类型转换拒绝。该模拟流程不代表现场提交验收。

- `quality-fixtures/quality-history-contracts.json`：现场 EDM、物理列、状态定义、继承与接口探测结论。
- `quality-fixtures/quality-history-read-validation.json`：现场 5 条固定查询的只读结果。
- `quality-fixtures/quality-history-summary.json`：本轮回归、网络隔离及尚未验收事项。

后续新增[受限批返工入口](quality-lot-rework.md)，但本历史候选/核对工具仍不支持lot_rework，不能用OK历史核对替代。ExecuteSamplingLot的ScrapReason标注当前版本未实现，批报废仍未开放。未执行 MES 业务写入。
