# 抽样批检验记录与整批OK执行

本轮接入CollectLotSamplingData，以及受限的ExecuteSamplingLot（仅OK=1）。后者只接受MES已经明确采样完成、采样通过的批，不由模型根据数值推断整批通过。返工2、报废3和通用质量放行不开放。

| 工具 | 功能 |
|---|---|
| prepare_lot_inspection | 从明确成员容器记录计数/数值测量结果，累加到明确批任务，只准备计划 |
| get_sampling_lot_readiness | 完整读取最多50个成员、50个任务，以及MES完成/通过标志，展示OK前置阻塞 |
| prepare_sampling_lot_completion | 准备MES已确认完成且通过的批OK执行，核对整个成员集合 |
| get_sampling_lot_history | 按历史SamplingLotId读取采样汇总和来源容器，不推断本次增量或最终因果结果 |

计划均由execute_quality_transaction确认后单次提交。执行先unknown，inspect_quality_transaction只给当前批及采样历史证据，不自动成功或解锁。仍需人工核对最终审计证据后review；这不等于已经接入自动因果核实。

## 批采样输入

计数现支持`counted_groups`显式互不重叠分组，仍按整批任务剩余数核对、一次计划一次请求；不能与根级计数或测量混用。子明细查询与原因载荷已按现场契约修正，实际累计及原因历史待验收。见[计数分组与现场回显](quality-counted-groups.md)。

以下为参数结构示例，不是本轮创建的数据：

```json
{
  "lot_name": "明确存在的抽样批",
  "container_name": "该批明确的来源容器",
  "sample_test_name": "明确存在的计数测试",
  "sample_test_revision": "A",
  "samples_collected": 3,
  "rejects": 1,
  "reject_reason_name": "该测试原因组内明确的原因",
  "comments": "本次检查说明"
}
```

samples_collected和rejects为**本次增量**。例如批已采2件、本次采3件，只提交3，不提交累计5，也不把某个成员容器的已采数或剩余数作为整批数字。每个计数计划支持一个拒收原因；拒收为0时不接受原因，非零时必须是测试实际原因组中的明确原因。

测量检验使用samples，不与计数参数混用：

```json
{
  "lot_name": "明确存在的抽样批",
  "container_name": "该批明确的来源容器",
  "sample_test_name": "明确存在的测量测试",
  "sample_test_revision": "A",
  "samples": [
    {"points": [{"data_point_name": "长度", "value": "9.8"}]},
    {"points": [{"data_point_name": "长度", "value": "11.0"}]}
  ]
}
```

复用[单容器检验](quality-inspection.md)的完整点值、字面数值范围和拒收计数校验。每个样本一个ServiceDetails项，多个点失败也只计一个失败样本；超限值如实保留，不代填默认值，不做单位换算，不把局部预检查当成MES最终判定。最多50个测量样本、20个点、一次100个点值，计数一次最多100，且均不得超过整批任务剩余数。

容器必须已进站、Active、冻结计数明确为0，并属于该批同一产品版本；不代做Move In。依据手册，SamplingRequired=false的成员也可作随机采样，但必须有明确的批任务。普通单容器提交工具仍拒绝带SamplingLotId的容器，不用普通DataCollectionDef代替批检验。

任务在LotSampleData上定位。同一测试有多个任务时，必须提供lot_sample_data_instance_id；可选lot_instance_id和container_instance_id进一步固定身份。任务若限定Spec，必须与当前容器工序ID一致。设备或供应商专属任务、全检/失败转全检、自动扣量/报废/生产事件配置、外部LIMS类型3尚不支持。

当前受限实现要求CompletedSamples与SamplesTested一致、Rejects与SamplesFailed一致；若不一致则停止，不能猜测继承/派生计数语义。任务必须属于同一批和计划，固定测试版本，并且未满足要求数、既有拒收未超过阈值。

请求明确设置MoveContainerOnCompletion=false、IsExternal=false和IsWebhook=false。预览核对批、成员、任务、固定测试、完整点值、类型8462/8463及整批计数；准备期间和执行前均重读批、任务和成员/工序状态。

## 整批OK执行

先用get_sampling_lot_readiness读取完整有界快照。只有以下条件均满足，才能准备：

- 批为Active，MES的SamplingCompleted和SamplingPassed都明确为true，且没有已有处置结果。
- 存在任务和成员；所有任务有正的要求数、已采满足要求、继承/派生计数一致、拒收不超MES阈值。
- 全部成员属于该批相同产品版本，Active且明确无冻结，数量为正，父子层级关系为空、子容器数明确为0。空冻结/子容器计数不自动当作0。
- 成员及任务各不超过50。超界直接拒绝，不用第一页或局部成员批准整批操作。

```json
{
  "lot_name": "MES已确认采样完成且通过的批",
  "comments": "整批结果执行说明"
}
```

以上传给prepare_sampling_lot_completion。工具固定SamplingLotResult=1（OK），SamplingCompleted=true与已读取的MES完成标志一致，不提供任意结果码、Spec、签名、流程覆盖或放行字段。未知额外参数会被MCP模式拒绝。execute=false还必须原样回显完整的Containers集合，不能遗漏或新增成员；预览后和提交前重新核对批头、所有成员及任务。

ExecuteSamplingLot是整批操作，具体后续处理由MES配置决定。本轮尚未现场验证其移站等后续影响，不能称为“仅更新批头”，也不能把它当作通用最终质量放行。实际影响范围与成员数量/数量字段完整展示在确认快照中，执行仍需系统确认并只发一次请求。

当前部署对ScrapReason的元数据注释标明该字段在当前InSite版本尚未实现；ReworkReason、Spec等字段仅说明类型，尚不足以证明正确的整批返工目标与处置规则。因此本轮不开放返工2或报废3，不通过逐容器返工、扣量或解除冻结模拟批处置。[现场契约与元数据](quality-fixtures/lot-execution-contracts.json)

## 防重、历史与连续流程

同批不同成员的累计提交共同占用该批；不能更换成员容器绕过unknown/in_flight。整批OK执行还占用全部成员，与同journal内的单容器检验、NCR及关联定义维护作冲突检查。旧的直接冻结/释放/返工工具、其他journal和外部客户端没有统一到这套占用，不把它当作MES全局锁。

正确顺序为：读批及测试定义 → prepare_lot_inspection → 展示并确认 → execute_quality_transaction → inspect提供证据 → 人工最终审计review → 重新读取就绪状态 → prepare_sampling_lot_completion → 展示全部成员并确认 → 单次执行。第一次请求即使HTTP成功且MES当前标志已变，也不能自动跳过原事务核实。

get_sampling_lot_history按CollectSamplingDataHistory.SamplingLotId关联历史，再按HistoryMainlineId及TxnId核对来源容器，避免用容器当前归属猜历史。返回历史原始汇总字段，不据总数推断本次增量，不提供逐点历史，也不构成ExecuteSamplingLot最终因果审计。空页、同值或评论标记都不允许自动重试或解除占用。

| 问法 | 应走流程 |
|---|---|
| “查询批采样历史。” | get_sampling_lot_history；路由到质量组，不误选普通采集历史 |
| “本次从C采了3件，裂纹1件，记录到批L。” | 明确测试版本/原因/必要任务ID，prepare_lot_inspection生成本次3/1计划，确认后单次提交 |
| “这个成员没有SamplingRequired，还能采吗？” | 确认已进站、批归属及任务；批随机采样允许false，不代进站 |
| “本地检查都没超限，直接把批设为通过。” | 不接受本地推断；必须读取MES完成/通过标志及完整任务、成员 |
| “刚才超时了，换个成员再提交。” | inspect原计划并核对最终证据；同批unknown继续占用，不换成员绕过 |
| “把整批报废/返工。” | 明确当前未开放，不降级为循环单容器处理 |

## 验证范围

最终全量回归1501项测试、2项子测试通过，质量专项295项，本轮新增68项；父pytest离线HTTPX真实网络尝试0：[摘要](quality-fixtures/lot-execution-summary.json)、[JUnit](quality-fixtures/lot-execution-junit.xml)、[输出](quality-fixtures/lot-execution-test-output.txt)、[网络报告](quality-fixtures/lot-execution-network.json)。

业务依据：本地Siemens Shop Floor 2510+ Rev.1手册PDF第373至378页（批采样规则、字段及计数/测量操作），以及本轮现场SDK、EDM和SQL列定义。MCP模拟会话分别覆盖计数、测量记录后的证据核对和整批OK执行；模拟操作员审计不代表已实现真实因果链自动核实。

现场目前没有SamplingLot样本。缺失目标拦截和固定历史关联SQL的空ID编译查询已验证，四个新增工具均注册；本轮没有创建抽样批、提交检验、执行整批业务或重启服务。完整目标预览、非空数据、真实结果、权限/签名及实际后续影响仍待现场验收。[只读证据](quality-fixtures/lot-execution-read-validation.json)
