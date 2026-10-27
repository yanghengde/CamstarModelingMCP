# 计数检验显式分组与现场回显修正

2026-10-26扩展已有`prepare_container_inspection`和`prepare_lot_inspection`，不增加独立提交工具；quality分组仍为41个工具。新增`counted_groups`用于明确分组计数，并修正单组/多组拒收原因载荷和子明细查询。

## 输入协议

```json
{
  "container_name": "明确存在的容器",
  "sample_test_name": "明确的计数测试",
  "sample_test_revision": "A",
  "counted_groups": [
    {"samples_collected": 3, "rejects": 1, "reject_reason_name": "裂纹"},
    {"samples_collected": 2, "rejects": 1, "reject_reason_name": "划伤"},
    {"samples_collected": 1, "rejects": 0}
  ],
  "comments": "互不重叠的本次计数分组"
}
```

以上为结构示例，不是现场数据。单容器用`prepare_container_inspection`；抽样批用`prepare_lot_inspection`并增加明确的`lot_name`，容器须是符合原批采样条件的已进站成员。

本例请求采样6、拒收2。每组的采样数**包括**该组的拒收数；不是将全局总数6重复写入每一组，也不是只给原因次数后由工具分摊。操作者必须明确互不重叠的分组，同一样本不能在多组重复。工具无法从计数证明样本实际互斥，`sample_partition_verified=false`。

- 1..20组，每组必须明确严格整数`samples_collected`和`rejects`，不代填零或默认数。
- 组内`1 <= samples_collected <= 100`，`0 <= rejects <= samples_collected`；本次采样合计最多100，且不得超过MES当前任务剩余数。
- 非零拒收组须明确该固定测试完整原因组中的一个原因；同一原因ID只能出现一次，不自动合并别名或重复组。
- 零拒收组不能指定原因，最多一个；测量`samples`及根级`samples_collected/rejects/reject_reason_name`均不能与分组混用。
- 不支持把同一件的多个缺陷次数直接当成多件拒收数；若仅有缺陷次数或全局总数，仍需要用户明确样本归属。

旧单组输入仍支持，参数位置保持兼容。非零拒收也使用本轮修正后的原因子明细格式。既有全检、扣量/报废、生产事件、外部LIMS及目标状态限制继续适用；不为了避免失败而裁剪拒收数或跳过失败记录。

## 现场接口发现与修正

只读现场探测发现：

1. 只展开`ServiceDetails($expand=SamplingDataPointDetails)`时，响应只返回子行引用，不能核对实际采样数/拒收数。
2. 在`ServiceDetails`中显式选择`CDOTypeId`，现场返回400，说明该属性没有被EDM发布；不能将模拟响应的这个字段作为必备条件。
3. 无子行时读取`CountedRejectReason`会访问`SamplingDataPointDetails[0].RejectReason`而报错。快捷原因输入在本次无目标非执行回显中没有呈现原因子行；显式子明细可回显数量、原因及标志。
4. 空子集合可被序列化器省略。零拒收组不请求原因子行；收到额外子行、缺少非空子行或分页时仍拒绝。

当前显式选择字段：

```text
ServiceDetails(
  $select=SampleTest,TotalSamplesCollected,Rejects,Rejected;
  $expand=SamplingDataPointDetails(
    $select=SampleDataPoint,DataValue,Passed,RejectReason
  )
)
```

实际URL中使用上述表达式的单行形式。非零拒收组保留自己的父级计数，并写入一个规范原因子行：

```json
{
  "totalSamplesCollected": 3,
  "rejects": 1,
  "rejected": true,
  "samplingDataPointDetails": [
    {"dataValue": "1", "passed": false, "rejectReason": {"name": "明确原因", "instanceId": "实际原因ID"}}
  ]
}
```

实际请求还包含固定版本`sampleTest`引用。计数子行的`dataValue`来自本组明确拒收数，不是补入测量默认值；不添加虚构的SampleDataPoint。全字段回显核对父计数、子数量、原因固定ID及列表完整性。

类型依据现场EDM中声明的`CollectSamplingDataDetails`和`SamplingDataPointDetails`契约。未公开运行时类型字段时允许缺省，`response_dynamic_type_verified=false`；若服务器额外返回了矛盾的CDO类型字段仍拒绝。不会通过ID前缀猜动态类型。

本轮也将两类检验的备注上限改为180个UTF-16单元并禁止NUL，事务标记另加，避免补充字符使备注超过实际存储容量。

## 执行与核对

多个分组进入**一个计划、一次HTTP提交**，不按原因循环执行多个事务。仍使用`execute_quality_transaction`的确认、重建摘要、关联占用和单次尝试机制；结果先unknown。

本次预览参数及载荷格式发生变化，升级前准备的ready计划可能被重建摘要判为过期变化，必须重新准备。已尝试的unknown计划继续保留占用，不能借重新准备重放。

分组只声明输入计数，`counted_group_accumulation_verified=false`、`counted_reason_history_verified=false`。尚未现场验证多组执行如何累计到MES任务和原因历史；不宣称质量通过、签名完成或允许放行。

历史核对新增`requested_sample_units`与`requested_rejects`展示请求合计；`detail_row_semantics`说明计数历史行是服务分组，不是逐件样本。旧字段`expected_samples/historical_samples`对计数仍表示明细行数，不能当作件数。计数历史缺少已核实的本次采样增量及原因归属证明，因此即使可比较字段一致也最多partial_match，不改状态、不解锁。

## 多轮问法

| 用户问法 | 正确处理 |
| --- | --- |
| “这次采6件，裂纹1件、划伤1件” | 不自动分摊；先明确各组采样数与互不重复的归属，再准备分组 |
| “第一组3件裂纹1，第二组2件划伤1，另1件无拒收” | counted_groups按3/1、2/1、1/0构造，合计6/2，一次计划 |
| “根级写6件，再加这些分组” | 拒绝混用，避免总数重复 |
| “同一裂纹原因再补一组” | 同次请求不重复原因，由用户明确正确分组；不自动合并 |
| “分组历史有三行，所以采了三件” | 说明三行是分组；请求件数与历史件数分别核实 |
| “提交超时，把第二个原因单独再提交” | 检查原unknown计划及证据，不按原因拆分绕过占用 |

## 证据与边界

新增57项测试；全量1781项测试、2项子测试通过，质量相关575项，离线父pytest进程的真实HTTPX网络尝试为0。覆盖单容器/批采样、严格嵌套参数、规范原因回显、现场捕获响应回放、旧格式计划防重放及真实FastMCP Client的模拟单次提交。问法验证检查工具可见性，未运行真实LLM多轮成功率评测。

- [现场契约和非执行探测](quality-fixtures/counted-groups-contracts.json)
- [回归摘要](quality-fixtures/counted-groups-summary.json)

两个采样接口均回显了无目标请求中的分组标量及规范原因子行；未提供真实容器/抽样批/测试目标，不能算完整目标预览或业务验收。无目标探测缺少的任务、移站和目标字段不会被补为默认值，真实计划仍拒绝不完整回显。全程未执行MES业务写入，未手动重启服务。批返工/报废、全检转换及SPC计算仍未完成。
