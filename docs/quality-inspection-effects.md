# 第三阶段：单容器原生全检与测量按样本数自动扣量

2026-10-28，扩展现有 `prepare_container_inspection`，质量工具数量仍为50。新增两个默认false的显式选项；它们互斥，不新增独立扣量执行入口。本阶段是受限首版，真实转换、内部扣量、签名及最终父子历史尚未验收。[高级功能计划](quality-advanced-roadmap.md)

| 参数 | 已开放范围 |
|---|---|
| allow_full_inspection=true | 按MES当前任务中已有的全检/失败转全检规则提交检验，支持满足当前全量要求的失败任务续检。不直接修改要求数或标志，不是任意强制转全检 |
| allow_automatic_quantity_loss=true | 仅数值测量SampleType=2，单独配置DecreaseBySampleSizeReason；由父检验事务内部按**本次ServiceDetails样本行数**扣量，不再提交一次ChangeQty |

默认行为继续拒绝全检、自动扣量和自动事件配置。只有用户明确允许对应原生处理时，代理才传true，不能为了绕过前置错误而自行打开。两个选项不能同时为true；若配置同时涉及全检和扣量，本首版拒绝。

## 原生全检规则

原任务必须明确返回InspectAllContQty、InspectAllQtyOnFailure，至少一项为true。当前数量须为正整数；若InspectAllContQty已为true，MES任务TotalSamplesRequired必须等于当前数量，客户端不自行纠正。

失败后续检还要求任务已经具有等于当前数量的全量要求；若拒收已超阈值但MES尚未形成该要求，直接拒绝。每次仍受MES当前任务剩余数限制，不能预计“这次失败后会扩到全量”就提前多报样本。

标准csi的 `CollectSampling_Execution` 实际逻辑是：

1. 更新检验累计计数。
2. 按配置执行内部数量损失，再处理自动生产事件。
3. 当Container不属于SamplingLot、MES SamplingStatus为Fail、InspectAllQtyOnFailure=true且InspectAllContQty=false时，设置TotalSamplesRequired为当时Container.Qty。

**尽管注释提到把InspectAllContQty设为true，这段CLF实际没有执行该赋值。** 因此工具保留MES实际标志，不伪造true，也不把当前全量要求视为某次MCP请求完成转换的因果证明。判断是否Fail及是否触发转换由MES决定，本地限值预检查不能代替它。

本轮不开放手工修改ContainerSampleData、不修改SamplingPlan以回填已有任务，也不把临时关闭配置当成全检转换。

## 自动扣量首版

只接受测量检验且以下配置同时满足：

- DecreaseBySampleSizeReason明确存在。
- DecreaseByRejectCountReason未配置，ScrapCountedRejectsByReason明确false。
- 不配置自动质量事件的DefaultFailureMode、Classification、SubClassification、DefaultPEDescription。
- 失效原因是LossReason，且属于当前Spec对应Operation完整LossReasons组。

标准 `CollectSampling_ChangeQty` 对测量检验使用 `GetListCount(ServiceDetails)` 作为损失量。因此3个样本、每样本2个点时，按3个主单位准备损失投影，不按6个点扣量。失败点数或拒收数不用于本模式的扣量计算；客户端不换算单位，预览展示MES当前主单位。

以下仍拒绝：计数检验自动扣量、按拒收数扣量、按不同拒收原因报废、两种扣量原因并存、扣至零或负数，以及全检与扣量组合。原生不同分支使用SamplesTested、FailedSamples或首行原因子明细，不能统一假定为本次样本增量。

父检验实际执行只提交一次CollectSamplingData。逐点数据及移站false保持原接口格式；**不会另发ChangeQty来补扣量**。预览中的ChangeQty调用全部是execute=false，仅核对子服务的类型、原因、目标、数量和默认值。

子服务必须回显LossDetails类型4030、方向-1、CountsAgainstProduction=true、RecordAllQty=false，并与本次样本数及原步骤一致。原生CloseWhenZero、AdjustThruput、GenerateRollups、DisassociateWhenClosed和RecordAllQtySummaryOnly默认值会展示、绑定进计划；不会把单独数量损失工具的关闭/生产数量选项偷偷传给检验子事务。首版拟扣量严格小于当前数量，避免归零处理。

标准CLF内部设置子ChangeQty.ProcessESignatures=false；这是被核对的原生内部行为，客户端不设置父检验的签名字段。不能据此称为签名链已验收，也不能用系统确认代替MES电子签名。

## 目标、依赖及执行限制

高级模式只处理基础Container类型1040：SamplingRequired明确true、SamplingPassed明确false、Active、冻结数明确0、未在返工、不属于SamplingLot、无父子容器/载具/第二单位或非零第二数量，主单位及产品ID明确。Batch子类型不开放。

读取固定版本测试和数值点，并以变更计数和内容摘要绑定；还核对当前Spec、Operation、原因组、损失原因和单位。Query工序/步骤与持久CurrentStatus必须一致，准备后重读目标、依赖及完整检验点集合，执行前再次重建摘要。跨请求核对不是MES事务快照或全局锁。

高级父检验非执行预览额外要求回显**原持久ContainerSampleDataItem任务ID**；即使计数相同，另一个任务也不能替代。根报文仅接受原检验字段、移站false，不允许附加qty、changeQty、全检标志或签名覆盖。

执行仍走现有 `execute_quality_transaction` 系统确认和单次提交。unknown时保留占用，不能重新检验或补发扣量。`inspect_quality_transaction`按原容器ID、原任务ID读取数量与当前任务，不跟随替换任务；当前数量或标志不能证明本次转换或内部扣量成功。

旧普通历史快照可能缺少移站选项，只读核对保留其未知，不补造false；它不能据此通过新的提交校验。

现有普通检验候选/载荷匹配没有核实这些原任务变化及内部扣量历史，因此 `find_quality_execution_candidates/reconcile_quality_execution` 对这两种高级计划明确拒绝。需最终原任务及父子事务证据人工review，不自动解锁。

## 调用结构示例

已有全检规则的计数检验，原有计数参数加：

```json
{"allow_full_inspection": true, "allow_automatic_quantity_loss": false}
```

已单独配置按样本数扣量的数值测量检验，原有samples参数加：

```json
{"allow_full_inspection": false, "allow_automatic_quantity_loss": true}
```

这不是现场业务数据；完整容器、测试名称/版本和本次样本仍必须明确。仅传开关不能创建或修改MES中的全检/扣量配置。

## 接口依据与验证状态

官方Designer组件只读核对的标准csi快照SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不代表部署自定义覆盖已核实。[原生执行与全检分支](quality-fixtures/inspection-effects-native-execution.json)、[扣量逻辑与目录](quality-fixtures/inspection-effects-native-index.json)、[字段定义](quality-fixtures/inspection-effects-native-fields.json)、[计数字段](quality-fixtures/inspection-effects-counted-fields.json)、[现场父检验契约](quality-fixtures/inspection-effects-collect-schema.json)

现场验证了真实容器上明确LossDetails、没有覆盖原生默认值的非执行回显；这不是实际扣量或合法检验正目标。父检验无目标探测及任务数量只读结果见[现场核查](quality-fixtures/inspection-effects-live-readcheck.json)，子服务见[原生默认值回显](quality-fixtures/inspection-effects-child-defaults.json)。

新增60项测试（原生检验59项、旧历史兼容1项）。质量相关1133项全部通过。全量2339项中2338项通过，1项非质量模块子进程等待超时；该项未改代码或时限单独复跑通过。2项子测试通过，两轮父pytest离线HTTPX真实网络尝试均为0。完整运行和复跑证据分别保留，不将其写成一次全绿。代码测试覆盖显式开关及互斥、默认保持拒绝、当前全量要求与失败续检、数值测量样本计数、原生子类型及默认值、余额保护、原因适用性、原任务ID、配置/数量变化、单次父检验提交、unknown不补扣量及MCP参数封闭。结果见[测试输出](quality-fixtures/inspection-effects-test-output.txt)、[JUnit](quality-fixtures/inspection-effects-junit.xml)、[网络记录](quality-fixtures/inspection-effects-network.json)、[阶段摘要](quality-fixtures/inspection-effects-summary.json)、[超时用例复跑](quality-fixtures/inspection-effects-retest-output.txt)。

本次未执行MES业务写入、修改业务配置、删除事务记录或重启服务。真实全检转换/续检、自动扣量及生产数量影响、父子历史、签名和实际LLM多轮选工具仍待验收；多成员抽样批报废、批高级检验、其他扣量分支、SPC及复杂审批仍未完成。
