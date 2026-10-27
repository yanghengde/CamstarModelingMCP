# 质量检验工具与执行范围

本文件介绍单容器AQL检验。检验定义维护、产品关联和抽样批创建见[定义与批工具](quality-definitions-lots.md)；批采样结果提交及MES已通过批的OK执行计划见[批检验工具](quality-lot-execution.md)。整批返工/报废和通用最终质量放行仍未实现。普通DataCollectionDef采集和这里的SampleTest不是同一套接口。

## 四个专用工具

| 工具 | 输入与结果 |
|---|---|
| get_container_inspection_tasks | container_name，按需明确container_instance_id；读取当前持久化ContainerSampleData、固定测试版本、说明、要求/已采/拒收数和剩余数。offset从0起，limit最多20 |
| get_inspection_test_definition | sample_test_name、sample_test_revision必填；展开最多20个检验点的类型/限值/单位及完整拒收原因组，展示扣量/事件配置 |
| prepare_container_inspection | 固定容器与测试版本，按计数或测量方式提供本次结果；核对任务和execute=false回显，生成有效1小时的计划，不执行检验 |
| get_container_inspection_history | 固定容器的历史汇总分页，返回MES原始计数、状态、TxnId和Comments；不包含逐样本逐点历史，不据历史总数推断本次增量 |

执行沿用execute_quality_transaction(plan_id,plan_digest)，本地查看用get_quality_transaction_plan，未知提交核对用inspect_quality_transaction。人工最终证据审计仍用review_quality_transaction，原计划不可重放。

参考数据查询增加sample_data_point、aql_level、loss_reason、loss_reason_group四种kind，可以通过list_quality_references和get_quality_reference_details读取。读取配置不等于配置已经适用于某个检验任务。

## 计数检验

以下名称均为调用结构示例，不是本轮创建的现场数据。先读取容器任务和测试定义，再准备：

```json
{
  "container_name": "明确存在的容器",
  "sample_test_name": "明确存在的计数测试",
  "sample_test_revision": "A",
  "samples_collected": 3,
  "rejects": 1,
  "reject_reason_name": "该测试原因组中的明确原因",
  "comments": "本次检查记录"
}
```

samples_collected和rejects都是**本次增量**。即使MES已采2件，本次采3件，也只提交3，不提交累计5或要求剩余8。两个参数都须明确整数，不能把布尔值、浮点数或数字字符串强制转成计数。

拒收数必须在0至本次采样数之间。旧单组参数的非零拒收要求一个明确配置的原因；零拒收不接受原因。多组可使用`counted_groups`，每组明确采样数、拒收数和非零拒收原因，不与根级计数混用，不自动分摊、合并或按原因循环提交。分组必须不重叠，拒收样本只记录一次。规范子明细及现场回显修正见[计数分组说明](quality-counted-groups.md)。

## 测量检验

```json
{
  "container_name": "明确存在的容器",
  "sample_test_name": "明确存在的测量测试",
  "sample_test_revision": "A",
  "samples": [
    {"points": [{"data_point_name": "长度", "value": "9.8"}]},
    {"points": [{"data_point_name": "长度", "value": "11.0"}]}
  ]
}
```

每个样本须完整提供该测试的全部点，不能遗漏、重复、多传或代填默认值。value为明确数值的字符串；只支持已核实的Integer=1、Float=2、Fixed=3、Decimal=9。上下限必须是字面数值，边界包含等号；不解释脚本、Spec表达式、时间、对象或外部采样结果。

工具按当前固定点定义做数值预检查，将每个样本放在一个ServiceDetails项，点值放在其SamplingDataPointDetails内。超限值仍如实进入预览，Passed=false；一个样本即使多个点超限，Rejects也只增加1。这个局部检查用于构造可审阅请求，**不代表MES已判定合格/不合格、完成审批或允许放行**。

最多50个样本、每个最多20个点，一次最多100个点值；请求还受22000字符预览长度限制。超过本次上限或MES任务剩余数时直接报错，不自动裁剪或分批执行。

## 提交前校验与结果语义

- 容器必须身份明确、Status为Active=1、明确需要采样、冻结计数明确为0，并且不属于SamplingLot。空冻结计数保留为未知，不自动当作0。
- 当前容器必须有唯一的该版本持久化检验任务；已满足要求数、既有拒收已超过MES阈值、全检或失败转全检配置均拒绝当前受限提交。工具不自行计算AQL表或补造任务。
- 拒绝已配置的按样本/拒收数扣量、按原因报废和自动生产事件配置；相关字段缺失或未知也不会当作已关闭。测试和检验点必须为明确Active状态。
- MoveContainerOnCompletion固定false，并要求服务原样回显。不向请求填入流程覆盖、签名或审批跳过字段。
- 预览核对全部样本、点值、引用、计数及已核实的明细CDO类型8462/8463；还要求RequiredSamples、SamplesTested、AllowedRejects、SampleType与任务/定义一致。
- 摘要绑定任务、容器业务状态、定义、原因组和已核实回显。Query/AdHoc的整数/布尔字符串仅按闭合类型转换；空值和未知编码不会冒充有效值。临时ServiceDetails/Parent和容器Inquiry实例ID不进入摘要。
- 确认后重新读取、比对摘要、原子领取，再只发一次execute=true。即使HTTP成功也先保存unknown；未知事务占用同一容器，不能改换检验测试或改名绕过。其他直接事务工具/外部客户端并不共享这一占用，不能把它当作MES全局锁。
- inspect只提供当前任务和历史汇总，不据空历史、当前值、评论标记或HTTP200归因成功，也不自动解除占用。异常和取消保存unknown，人工核对最终证据后审计。

## 真实问法与调用顺序

| 问法 | 应执行的步骤 |
|---|---|
| “这个容器现在要做哪些检验？” | get_container_inspection_tasks；只对当前持久化任务页回答，不称为全部工序待办 |
| “这个测试的点、单位和上下限是什么？” | 用任务返回的名称和版本调用get_inspection_test_definition |
| “本次检了3件，裂纹1件，帮我登记。” | 核对固定任务/版本和明确组内原因；prepare_container_inspection生成本次3/1的计划；展示并经系统确认后执行 |
| “已采2件，这次又采3件，要填5吗？” | 说明本次提交为3；不得据累计数代替用户填写本次结果 |
| “这个样本两个点超限，也记录下来。” | 完整保留全部点值及失败预检查；该样本只计1个拒收；确认后提交，不丢弃失败样本 |
| “刚才超时了，再提交一次。” | 检查原计划并inspect；保留unknown，不能重放或另建计划绕过 |
| “帮我建抽样批/提交批抽样/自动报废。” | 建批用prepare_sampling_lot_create，批结果用prepare_lot_inspection；报废和通用放行未开放，不模拟成逐容器处理 |

表中为预期工具流程；本轮验证包含MCP客户端准备/确认/执行/核对的模拟会话，尚未完成真实LLM多轮与现场MES联合验收。

## 契约与现场证据

最终全量回归1385项测试及2项子测试通过，质量专项179项，较上一轮新增86项。MCP模拟MES会话覆盖准备、确认策略、单次提交和未知结果核对；父pytest离线HTTPX真实网络尝试0：[JUnit](quality-fixtures/inspection-junit.xml)、[测试输出](quality-fixtures/inspection-test-output.txt)、[网络报告](quality-fixtures/inspection-network.json)、[摘要](quality-fixtures/inspection-summary.json)。模拟验证不代表现场检验验收。

本轮读取SampleDataPoints、SampleTests、SamplingPlans、AQLLevels、LossReasonGroups的现场Swagger及Modeling EDM，核对固定SQL列与DataType选择值：[契约归档](quality-fixtures/inspection-contracts.json)。业务依据为本地Siemens Shop Floor 2510+ Rev.1手册PDF第359、361至364页及SampleTest EDM：[页码与摘要](quality-fixtures/inspection-manual-notes.json)。

当前现场SampleTests为空，SN0000001没有检验任务或历史。任务和历史SQL查询已跑通；对无测试目标的execute=false探测被MES以SampleTest为空拒绝，没有生成可执行计划：[只读证据](quality-fixtures/inspection-read-validation.json)。不能把这个探测当作完整目标预览通过。

本轮没有创建、更新、检验或关闭真实MES数据，也没有重启服务。非空定义、完整目标预览、真实计数/测量提交、逐点历史链、账号权限、签名、批抽样和最终质量放行仍待验收或后续实现。
