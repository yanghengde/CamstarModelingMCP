2026-11-04 更新。已在 D01 核实双单位部分损失，在 A01 核实单单位全量损失与归零关闭，两笔事务均已核对损失历史和生产数量选项。整批结果 3 已在 LS 核对。[隔离验收记录](c4-isolated-acceptance.md)

# 数量损失准备与抽样批报废前置检查

2026-10-28，高级功能第二阶段继续开发，质量分组由48增至50个工具。本轮实现**单个基础Container的部分数量损失或明确全量损失准备**，以及**不能执行的抽样批报废前置检查**。多成员抽样批报废仍未完成，没有开放ExecuteSamplingLot结果3，不能将本轮称为全部批报废交付。[开发计划](quality-advanced-roadmap.md)

## 两个工具

| 工具 | 已实现范围 |
|---|---|
| prepare_container_quantity_loss | 原生ChangeQty配合明确的LossDetails子类型；绑定当前数量、单位、工序、Operation原因组，生成确认后单次执行的质量计划 |
| get_sampling_lot_scrap_readiness | 完整读取最多50成员/50任务，重读一致，展示原生分支会跳过的成员及接口阻塞；ready_for_execution固定false，不创建计划、不写MES |

先区分对象类型，不能仅根据用户说“批次”就选择整批事务：

| 实际对象 | 本轮处理 |
|---|---|
| 基础Container，CDO类型1040 | 符合下列限制时，可准备单容器数量损失；业务名称可以是批号，但这不改变它的对象类型 |
| Batch，CDO类型7939及其他Container子类型 | 本工具拒绝。标准ChangeQty_Validate也明确拒绝Batch，不能把基础Container工具直接推广到它 |
| SamplingLot，MfgLot表中CDO类型8587、多个成员Container | 只读报废前置检查；成员不能通过单容器工具循环扣量替代整批报废 |
| PartScrap | 与资源及scrapQty相关，不是本轮Container损失的替代入口 |

## 数量损失参数

| 参数 | 要求 |
|---|---|
| container_name | 明确的基础Container名称 |
| loss_reason_name | 明确LossReason名称，必须属于当前Spec对应Operation的完整LossReasons组；不使用同名ScrapReason代替 |
| close_when_zero | **必填布尔值**，数量归零时是否关闭；不替用户选择 |
| adjust_throughput | **必填布尔值**，是否调整已处理生产数量；对应原生AdjustThruput，不等同于质量批准或工单完工 |
| loss_quantity | 部分损失时必填的正数**字符串**，当前主单位下的本次增量，不是目标余额，不自动换算单位 |
| record_all_quantity | 全量损失明确传true并省略loss_quantity；两种模式必须且只能选一种，部分模式不得隐式传入恰好等于当前数量的值 |
| container_instance_id | 可选，固定16位十六进制ID |
| comments | 可选，最多180个UTF-16单元；系统另加原请求关联标记 |

例如“损失1.5件、归零不关闭、不调整已处理生产数量”的参数结构如下。这是结构示例，不表示本轮创建或扣减了此容器。

```json
{
  "container_name": "明确的容器",
  "loss_reason_name": "当前Operation允许的LossReason",
  "close_when_zero": false,
  "adjust_throughput": false,
  "loss_quantity": "1.5"
}
```

全量模式改用 `record_all_quantity=true` 并省略 `loss_quantity`。实际报文不设置qty或qty2，由原生RecordAllQty处理；预览另展示当前量、拟损失量、当前主单位和预计余额。预计余额、预计关闭均为本地投影，不是执行结果。当前量与损失量受限于1e12及数值传输精度，不舍入或接受会被忽略的微小扣量。

限制：Active、冻结数明确为0、未在返工，无父子容器或载具；默认要求无第二单位/非零第二数量，双单位明确部分损失见文末扩展，主单位和产品ID明确，且不属于SamplingLot。未知冻结数不能当0，未知单位不能当“件”。当前Spec固定版本Active，Query工序/步骤与持久CurrentStatus一致。

工具固定不生成层级rollup、不解关联、不仅写汇总；不提供任意服务、数量类型、正负方向、电子签名、采样任务或自定义报废标志覆盖。计入生产的语义使用原生LossDetails回显并明确展示。部分数量损失和全量损失不能自动证明工厂完整报废审批已完成，也不等于自动检验扣量。

## 原生类型核查与旧入口修正

现场非执行探测得到两个不同结果：

| 请求 | 原生回显 | 处理 |
|---|---|---|
| 普通明细，仅changeQtyType=2 | CDOTypeId=4010、QtyMultiplier=+1、CountsAgainstProduction=false | 不接受为损失计划，不能假定数字2会自动创建正确子类型 |
| 明确@odata.type=#shopfloor.LossDetails，changeQtyType=2 | CDOTypeId=4030、QtyMultiplier=-1、CountsAgainstProduction=true | 新工具必须核对这一组合、目标、原因、数量模式及全部输入回显 |

新准备工具必须使用明确的OData子类型。服务器未在OpenAPI的明细列表中完整列出此派生类型，但EDM发布了LossDetails继承关系，非执行目标请求已验证可正确解析。[现场类型对比及EDM](quality-fixtures/quantity-loss-contract-probes.json)、[目标非执行回显](quality-fixtures/quantity-loss-target-probe.json)、[ChangeQty契约](quality-fixtures/lot-scrap-changeqty-schema.json)

既有 `change_container_quantity(change_qty_type=2)` 现在直接拒绝，提示使用新准备工具和统一确认执行，不再发送普通4010损失请求，也不偷偷把旧调用转换成另一个实际写入。其他既有数量类型保持原范围和待验收状态。损失/报废问法会路由到quality，隐藏这个旧数量入口，避免模型回退。旧入口变更见[生产基础工具说明](production-basics-tools.md)。

非执行回显中现场还返回 `ES_Scrapped=false` 等自定义字段。新工具只展示已返回的该字段，不代填、不推断其业务作用，`custom_scrap_process_verified=false`。原生字段回显不代表模块审批、签名或工厂自定义报废流程已核实。

## 抽样批报废的历史限制与本轮接入

官方Designer元数据组件只读核对的标准csi快照SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不是部署自定义覆盖证明。

- ExecuteSamplingLot_ExecuteScrap确实存在：创建ChangeQty子服务、创建LossDetails、类型2、RecordAllQty=true、设置原因、执行后清理成员ContainerSampleData；外层处理结束还标记批完成及结果3。
- 入口ScrapReason字段仍明确标注“当前版本未实现”。它引用ScrapReason（CDO类型4170），而LossDetails.ReasonCode要求LossReason（4140）；二者同为UserCode的独立子类型，不是相互继承。标准脚本直接赋值不能证明现场类型兼容或存在明确业务映射，更不能按名称相同自动转换。
- 映射ExecuteSamplingLotToChangeQty只转发Employee、Factory、TxnDate、TxnDateGMT、User，不转发关闭/数量/评论选项。不能把根层参数或单独ChangeQty的行为假定为整批子服务行为。
- 部署覆盖、全量扣量和归零关闭、多成员原子性、成员抽样明细清理及最终历史均无正目标验证。

2026-11-04现场Load已验证显式LossReason持久ID可以进入ScrapReason槽并回显真实CDOTypeName=LossReason。新增prepare_sampling_lot_scrap据此逐次核对类型，而不是按名称替换。get_sampling_lot_scrap_readiness可带loss_reason_name运行同一前置检查，正式结果3计划使用独立入口。原子性、关闭、清理及最终历史仍需隔离现场验收。上述类型声明保留为历史依据，当前接入详见[整批报废说明](quality-lot-scrap.md)。

## 单次执行与未知结果

流程：查询身份/数量/原因 → prepare_container_quantity_loss → 展示原量、损失量、主单位、关闭与生产数量选项 → execute_quality_transaction系统确认并单次提交。准备后重读，执行前重建摘要；容器/数量/当前状态、Spec、Operation、原因组、原因、单位或配置变更均阻止旧计划提交。

正式提交只发一次ChangeQty，不自动重试。即使HTTP200，结果先unknown，原容器与配置保留占用。`inspect_quality_transaction`按原ID读取当前记录，不通过旧名称找另一个对象；当前数量符合投影或状态Closed都不是本次提交成功的因果证据。未返回记录也不证明容器不存在或请求未提交，因为查询还依赖当前状态记录。

目前 `find_quality_execution_candidates/reconcile_quality_execution` 不支持quantity_loss，未猜测历史表/父键，不伪造最终历史。最终审计证据须人工review，不能换一个新计划再次扣量。占用只覆盖共用质量journal的流程，不是MES全局锁，不能承诺阻止外部客户端或其他既有数量类型的写入。

## 验证与未完成范围

新增96项专项测试；全量2279项、2项子测试通过，质量相关1073项，父pytest离线HTTPX真实网络尝试0。本地协议核对50个质量工具、根参数禁止额外字段、6个质量写入/审计入口均要求确认。代码测试覆盖类型/方向、数量模式、明确关闭与生产数量选项、组适用性、单位和容器限制、精度、配置/余额变化、旧入口阻断、路由、MCP参数封闭和确认、单次执行、unknown占用及原ID检查；批报废检查不创建计划、不写MES。最新结果见[测试输出](quality-fixtures/quantity-loss-test-output.txt)、[JUnit](quality-fixtures/quantity-loss-junit.xml)、[网络记录](quality-fixtures/quantity-loss-network.json)、[阶段摘要](quality-fixtures/quantity-loss-summary.json)。

现场已验证明确LossDetails在真实容器上的非执行回显。已查目标缺少可确认的冻结数、主单位，已查Operation的LossReasons配置为空，因此未将其当成完整可执行正目标，没有创建业务准备计划或修改配置。[目标与配置只读结果](quality-fixtures/quantity-loss-live-readcheck.json)、[持久状态列](quality-fixtures/quantity-loss-currentstatus-columns.json)、[原生校验与执行逻辑](quality-fixtures/quantity-loss-native-logic.json)

本轮没有MES业务写入、删除事务记录、部署或重启服务。新进程本地注册不代表运行服务已重新加载。真实数量损失/关闭、生产数量影响、自定义报废流程、签名、最终历史仍待验收；多成员抽样批报废、检验自动扣量、转全检、SPC和复杂审批仍未完成。


## C4双单位部分数量损失

复用prepare_container_quantity_loss，增加可选secondary_loss_quantity。主数量和第二数量分别由用户明确，保持各自现有单位，以一次ChangeQty的同一LossDetails.qty/qty2提交。该范围适用于基础Container，SamplingLot成员和Batch子类型继续按原校验处理。

双单位模式要求两个原余额均为正，两个增量各自为正且严格小于对应余额，第二单位持久ID有效。主/第二数量均执行既有数值范围和无损传输检查；全量模式与明确第二增量不能混用。省略第二增量保持原流程及原限制。

准备固定两种单位配置及其版本、原余额、原因和工序，完整回显核对原生LossDetails类型4030、方向-1及Qty/Qty2。变化使旧计划失效。预览分别显示两组损失、预计余额与单位，unit_conversion_performed=false。确认后仅一次ChangeQty，unknown保持占用。

inspect增加两组原单位是否一致和当前余额是否匹配投影。余额相符作为人工核实材料，最终事务因果链仍未自动认证。真实双数量变更、生产数量与历史影响仍待现场验收。

本轮相关回归250项通过，新增双单位专项24项，离线HTTPX真实传输尝试0。原生LossDetails.Qty2字段用途已通过官方Designer组件只读核对，当前实现仍依赖实际目标的非执行回显。整批报废原因类型兼容和原子性继续作为C4剩余阻塞项。
