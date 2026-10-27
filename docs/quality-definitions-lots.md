# 检验定义维护、产品关联与抽样批

2026-10-26新增四个准备/查询工具。执行仍统一使用execute_quality_transaction，经确认、重新读取和摘要比较后只提交一次。Modeling定义维护只有静态契约校验，不存在已验证的execute=false回显；不能把准备成功称为服务端业务批准。

| 工具 | 范围 |
|---|---|
| prepare_inspection_definition | sample_data_point、sample_test、sampling_plan三类定义的create/update/revise计划，固定名称/版本 |
| prepare_product_sampling_plan | 固定产品版本直接关联固定SamplingPlan版本，仅修改SamplingPlan引用 |
| get_sampling_lot_details | 读取抽样批、成员容器及LotSampleData，成员与样本分别分页 |
| prepare_sampling_lot_create | 明确批名、产品版本、数量与单位，准备CreateSamplingLot并核对execute=false回显 |

## 定义维护参数

prepare_inspection_definition的公共参数为definition_kind、action、name、revision、changes；仅revise还须明确source_revision。

| definition_kind | changes允许字段 |
|---|---|
| sample_data_point | description、status、data_type、lower_limit、upper_limit、default_value、show_limits、uom_name |
| sample_test | description、status、sample_type、sample_data_points、instructions、reject_reason_group_name、allow_move_on_failure |
| sampling_plan | description、status、aql_level_name、inspection_level_name、sample_rate、move_container_on_completion、sample_size_based_on_start_qty、tests |

status仅接受整数1/2；布尔值不会转换成状态数字。data_type限integer/float/fixed/decimal，sample_type限counted/measured。名称和版本引用必须明确；不默认使用ROR。新对象默认Active，测试默认不允许失败后移站且不按原因报废；新计划默认不自动移站、采样频率1且不基于开工数量。参数未涉及的已有字段由PUT保留，不自动清空。

创建要求该名称尚无任何版本。update通过对象ID和显式revision参数锁定版本，允许维护可修改的非ROR版本。revise使用PATCH，由MES复制当前ROR；source_revision必须是经读取确认的当前ROR，目标版本必须不存在。冻结对象拒绝维护；不提供定义整体删除或重命名。

字符串上下限必须是字面数值，不解释Spec表达式。上下限反置、非数值默认值或不适用字段会报错。显式null可清除已支持的可空字段，布尔配置和状态等不能清为空值。

以下均为参数结构示例，不是本轮实际创建的数据；引用名称必须以现场查询结果为准。

```json
{
  "definition_kind": "sample_data_point",
  "action": "create",
  "name": "长度检验点",
  "revision": "A",
  "changes": {
    "data_type": "decimal",
    "lower_limit": "9.5",
    "upper_limit": "10.5",
    "show_limits": true,
    "uom_name": "现场已存在的毫米单位名称"
  }
}
```

计数测试不接受测量点；测量测试必须关联完整且唯一的Active数值点清单。例如：

```json
{
  "definition_kind": "sample_test",
  "action": "create",
  "name": "尺寸检验",
  "revision": "A",
  "changes": {
    "sample_type": "measured",
    "instructions": "按作业指导书记录实际测量值",
    "sample_data_points": [{"name": "长度检验点", "revision": "A"}]
  }
}
```

sample_data_points是明确的完整替换清单；更新时先按原索引降序删除再追加明确的新引用，未传入则保留原清单。计数/测量类型不在原定义上直接互换；不提供外部采样类型3维护。当前工具不维护AQL表内容、检验级别数量范围或自动生产事件配置。

## 抽样计划明细

tests是最多50项的明细增量，op为add/update/remove，sample_test固定名称与版本。相同测试出现在多个明细时，维护必须额外明确detail_instance_id，不任意选择一行。

```json
{
  "definition_kind": "sampling_plan",
  "action": "create",
  "name": "尺寸抽样计划",
  "revision": "A",
  "changes": {
    "aql_level_name": "明确存在的AQL级别",
    "inspection_level_name": "明确存在的检验级别",
    "sample_rate": 5,
    "tests": [{"op": "add", "sample_test": {"name": "尺寸检验", "revision": "A"}}]
  }
}
```

明细可设置aql_level_name、inspection_level_name、sample_rate、sequence、inspect_all、inspect_all_container_qty、inspect_all_qty_on_failure、spec和resource_name。新增明细默认不全检、不失败转全检且不冻结；明细未指定采样频率时使用本次根设置或已确认的现有根频率，不悄悄改成每个容器都采。

维护会先处理原索引更新、再按降序删除、最后追加，避免删除导致后续索引错位。同一行不能通过测试引用和明细ID两个别名重复修改。删除只接受定位字段，不夹带其他设置。新建计划必须有明细；空tests不能隐式清空计划。

此处可维护完整的计划配置；当前单容器检验执行工具仍拒绝全检转换、自动扣量/事件配置，并固定移站false。不能因计划中设置了这些选项就声称MCP执行端已支持。

## 产品关联

```json
{
  "product_name": "明确存在的产品",
  "product_revision": "A",
  "sampling_plan_name": "尺寸抽样计划",
  "sampling_plan_revision": "A"
}
```

以上传给prepare_product_sampling_plan。只写该版本产品的直接SamplingPlan引用，保留其他配置；不解绑继承设置，不回填、重建或覆盖已有容器持久化的检验任务，不据关联成功就认定当前容器已获得检验任务。

## 抽样批创建与读取

```json
{
  "lot_name": "来料抽样批示例",
  "product_name": "明确存在的产品",
  "product_revision": "A",
  "quantity": "100",
  "uom_name": "明确存在的单位",
  "allow_adjust_to_lot_size": false
}
```

以上传给prepare_sampling_lot_create。数量为正数值字符串，上限1e12；转换为接口Double时不能损失文本数值精度。产品必须Active，工具从其实际解析引用固定抽样计划，要求计划及测试Active且有明细。可选sample_rate必须为正整数；填写后会覆盖该批计划各项采样频率，因此不能自动挑选默认值。allow_adjust_to_lot_size显式为true时，允许MES依据后续实际容器数量调整样本大小；默认false。

CreateSamplingLot创建的是批，不是生产容器。containers是只读字段，工具不接受或写入容器清单，不开工、不自动挂接现有容器。后续容器如何进入抽样批属于独立流程。后续新增的CollectLotSamplingData记录及受限ExecuteSamplingLot（仅MES已通过批OK）见[批检验工具](quality-lot-execution.md)；不提供整批返工/报废。

get_sampling_lot_details通过已核实的MfgLot存储读取，并限定SamplingLot类型8587。lot_name或lot_instance_id定位，container_offset和sample_offset分别控制成员、样本页；limit=1..100。两部分各自返回has_more，不把空页当作批合格或创建失败。

## 占用与证据

维护计划绑定明确版本、前值、数据库ChangeCount和引用；执行前重新读取并比对。Modeling严格使用POST创建、PUT更新、PATCH升版；更新还固定revision查询参数，不走Shopfloor execute=true。

新维护、产品关联和建批共用质量journal。相关定义存在unknown/in_flight时，不能通过检验、关联或建批绕过占用；反向也一样。独立容器不会因共享只读定义而产生持久化占用冲突，实际写请求仍经本地互斥串行化。名称族及已知对象ID用于关联检查，保守覆盖不同版本。外部客户端和其他journal不共享这一占用，不能当作MES全局锁或原子版本比较。

HTTP成功、当前配置相同或同名批存在均不归因本次成功。执行结果先unknown，inspect仅收集当前证据，仍须人工最终审计证据才能review。尝试写入后的原计划不能重放，取消和超时保留unknown及原响应。

最终全量回归1433项测试及2项子测试通过，质量专项227项，本轮新增48项；父pytest离线HTTPX真实网络尝试0。覆盖MCP模拟Modeling与建批会话、方法与固定版本路由、原索引删除顺序、读取依赖冲突及中断恢复：[摘要](quality-fixtures/definition-lot-summary.json)、[JUnit](quality-fixtures/definition-lot-junit.xml)、[测试输出](quality-fixtures/definition-lot-test-output.txt)、[网络报告](quality-fixtures/definition-lot-network.json)。

现场证据见[契约及列定义](quality-fixtures/definition-lot-contracts.json)、[只读与静态准备](quality-fixtures/definition-lot-read-validation.json)。本轮重新读取了四份Swagger；检验点静态准备成功，重复重建摘要一致，测试计划在本地取消，回查目标未创建。建模未执行execute=false服务预览；不存在的抽样批查询只证明查询可用，不证明非空详情或创建成功。

业务依据为本地Siemens REST API 2510+ Rev.1手册PDF第78、85至94页，以及Shop Floor手册第368至369页。真实Modeling写入、非空抽样批完整预览/创建、账号许可、签名及最终审计链仍待现场验收；本轮未写MES，也未重启服务。
