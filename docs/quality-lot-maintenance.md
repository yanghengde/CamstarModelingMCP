# 抽样批数量与采样频率维护

2026-10-26新增2个MCP工具，quality分组共41个工具；既有候选查询、原请求核对与unknown审计也支持这类维护。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `prepare_sampling_lot_update` | `lot_name`，至少一项`new_quantity` / `new_sample_rate`，可选`lot_instance_id`、`comments` | 准备`UpdateSamplingLot`单次维护计划，不写MES |
| `get_sampling_lot_maintenance_history` | `lot_name`，可选`lot_instance_id`、`offset`、`limit` | 按历史批ID查询保存的数量、频率、评论、事务及冲销状态 |

手册《Shop Floor User Guide》2510+ Rev.1第8-14至8-16节（PDF第370–372页）说明Update Sampling Lot可更新批数量及采样频率，Containers为只读成员清单。现场Swagger实际输入字段为`qty`、`sampleRate`、`samplingLot`，并非自行推测的newQty等字段。`SampleRate`表示每第n个容器采样，不能当作单次测试样本量。

## 支持范围

只支持以下可完整核对的当前状态：

- 批类型为SamplingLot（8587）、LotStatus为Active，MES的SamplingCompleted和SamplingPassed均明确false，未有处置结果。
- 存在1..50个完整批检验任务，要求样本数为正，CompletedSamples、SamplesTested、Rejects、SamplesFailed均为0。
- 没有Active（ReversalStatus=1）的批采样历史；当前计数为0本身不能证明历史未采样。工具不宣称从未发生过已冲销采样。
- 最多50个完整成员，名称/ID唯一、产品版本与批相同、Active、无冻结且数量为正；暂不支持层级容器。
- 不处理全检、失败转全检、外部采样类型或已有/未完整展开的LIMS样本数据。

允许明确无成员的批，服务回显成员也必须为空；不会自动创建或挂接容器。已有任何有效采样的批、完成或失败待处置的批都不通过本工具强改要求数。

数量使用明确正数字符串，最大1e12，校验接口Double文本往返无精度损失。新批数量不得小于完整成员主数量合计，不自动忽略成员或触发缩量后的回调。频率为1..2147483647的严格整数，不接受字符串、浮点数或布尔。没有实际数量或频率变化时不生成计划。

未修改的数量/频率保留当前值，原AllowAdjustToLotSize明确保留并写入预览；工具不接受修改这个选项，也不接受成员、计划、结果码、审批、移站等覆盖字段。备注最多180个UTF-16单元，事务关联标记另加，避免备注截断关联标记。

## 示例与顺序

```json
{
  "lot_name": "LOT-001",
  "new_quantity": "120",
  "new_sample_rate": 3,
  "comments": "检验开始前按已确认批量调整"
}
```

将以上参数传给`prepare_sampling_lot_update`。也可只传新数量或新频率；预览会显示另一项的保留值及保留的批量调整选项。

准备时完整读取批头、成员及任务，执行`UpdateSamplingLot?execute=false`，核对数量、频率、批引用、评论、原计划、当前频率、完整成员集合和原任务的固定引用/计数。任务外部数据必须完整展开为空。SDK省略空引用字段时，仅在独立SQL快照明确该外键为空的条件下接受，不把未知引用当空引用。

预览中的任务数是**加载的原任务**。`projected_task_requirements_verified=false`，不预测执行后的RequiredSamples、AllowedRejects或成员SamplingRequired，也不替MES计算或批准新的检验要求。计划展示完整before和影响成员集合，随后再次读取批、成员、任务及有效历史检测变化。

审阅预览后通过`execute_quality_transaction(plan_id, plan_digest)`确认单次提交。提交前再次重建计划并比较摘要；只发一次HTTP请求，无重试。结果先unknown，保留整批和成员占用，同时与抽样基础配置维护保持保守互斥。同批采样、OK执行或对相关成员的质量写入不能切换计划绕过占用。

这些检测不提供服务端条件写入或原子数据库快照；外部客户端可能在最后一次读取后更改成员/任务，仍需现场并发验收。

## 维护历史与结果核对

`get_sampling_lot_maintenance_history`按`UpdateSamplingLotHistory.SamplingLotId`查询，并通过HistoryMainlineId及TxnId连接主历史。返回保存的Qty、SampleRate及`StoredComments`，不将它们解释为变化增量、旧值或执行后的任务重算结果。分页仅是当前页，不证明完整或唯一。

unknown计划可以依次调用：

1. `inspect_quality_transaction`读取当前批与维护历史，仅提供证据。
2. `find_quality_execution_candidates(plan_id, plan_digest)`按历史批ID及维护汇总中保存的完整评论精确筛选；不假定主历史Comments一定与汇总相同。
3. `reconcile_quality_execution(plan_id, plan_digest, candidate_txn_id)`核对明确候选的批、数量、频率、类型8592和Active状态；多个汇总候选不拼成一次维护。

即使这些字段一致，维护历史没有保存AllowAdjustToLotSize，成员标志及任务重算也未验收，因此最多`partial_match`。`verified_success=false`、`state_changed=false`、`retry_allowed=false`，不自动解锁。需要原有`review_quality_transaction`的最终人工审计证据。

## 多轮问法

| 问法 | 调用及结果要求 |
| --- | --- |
| “看看批L的数量和频率” → “数量改成120，频率保持” | 详情读取 → prepare只传new_quantity；预览保留原频率，不默改为1 |
| “把批L采样频率调整为3” | prepare传new_sample_rate=3；显示原数量与影响成员 |
| “采过一部分了，把批量改大” | 返回范围阻塞；不重置采样数、不通过新计划绕过 |
| “当前计数都是0，可以改吗” | 还须查有效采样历史、批状态、完整成员与任务；不只看零计数 |
| “提交超时了，重复一次” | 查原计划与候选事务；不重放unknown请求 |
| “历史里数量频率都对，自动解锁吧” | 只读核对最多partial_match，仍需最终证据审计 |

## 现场验证边界

本轮新增83项测试；全量1724项测试、2项子测试通过，其中质量相关518项。离线父pytest进程的真实HTTPX网络尝试为0。真实FastMCP Client模拟了准备、单次提交、unknown检查、候选查询及原请求核对；相同历史值不会自动解除占用。

已读取现场Swagger、EDM与历史表字段；非执行的无目标服务探测返回HTTP200，但仅回显默认选项，不能算正向目标回显。四条只读SQL均通过；现场抽样批及维护历史仍为空。尚未验证真实维护、任务重算或成员采样频率影响，未执行MES业务写入，未手动重启服务。

证据：[接口与字段](quality-fixtures/lot-maintenance-contracts.json)、[现场只读探测](quality-fixtures/lot-maintenance-read-validation.json)、[回归摘要](quality-fixtures/lot-maintenance-summary.json)。后续新增[受限批返工](quality-lot-rework.md)，其扩展及验收、批报废、全检转换、自动扣量及通用质量放行仍未开放。
