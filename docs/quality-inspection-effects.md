# 单容器原生全检与自动扣量

同一样本组现在可以明确多个互不重叠的拒收原因。使用 `samples_collected` 指定本组样本总数，`counted_reject_reasons` 中逐项给出 `reject_reason_name` 和正整数 `rejects`。省略根级 `rejects`、`reject_reason_name` 和 `counted_groups`。各原因对应不同拒收件，合计不得超过样本总数，最多20个原因、100个样本。

例如本组检验4件，其中1件原因为 A、2件原因为 B，父检验提交一条样本组明细和两个原因子项。原生扣量子预览同时回显两条 LossDetails，分别核对原原因及1、2件扣量，预计总损失3件。每个原因均绑定检验原因组、工序损失原因组及 LossReason 持久ID。任意明细缺失、数量冲突或配置变化均拒绝原计划。确认页面逐原因显示件数，正式仍执行一次父检验。

本轮核心扩展全项目 4637 项测试及 2 项子测试通过。最终预览元数据和原因数展示补充后，187 项定向回归通过。两次离线 HTTPX 网络尝试均为 0。各原因互不重叠由用户明确，预览保留 `sample_partition_verified=false`。实际多原因扣量和最终历史仍需现场样本验收。记录位于本地 `docs/quality-fixtures/quality-multiple-reasons-full-junit.xml` 和 `quality-multiple-reasons-final-junit.xml`。

本次接入单组按拒收原因报废。测试须仅配置 `ScrapCountedRejectsByReason=true`，本次样本数、正数拒收数和原因明确，用户允许原生自动扣量。拒收原因同时通过检验 `AQLRejectReasons` 完整原因组、当前工序损失原因组和 LossReason 持久身份核对。准备、父预览后复查及执行前重建均固定原因组版本。正式只提交一次父检验事务。

标准原生扣量分支遍历 `ServiceDetails[0].SamplingDataPointDetails`。当前计数输入每组对应一条检验明细，因此该模式要求一个正数拒收组，避免多组被部分扣量。其他计数扣量模式保留已有分组支持。按原因报废可组合已有全检规则，独立归零仍须明确归零许可及原生关闭默认。现场正向效果仍待具备配置的隔离样本验收。

本次全项目 4622 项测试及 2 项子测试通过，真实 HTTPX 网络尝试为 0。现场只读确认父检验提交和点级原因输入契约可用，当前按原因报废测试配置为零。测试及现场记录位于本地 `docs/quality-fixtures/quality-reason-loss-full-junit.xml`、`quality-reason-loss-full-network.json` 和 `quality-reason-loss-live-readcheck.json`。

2026-11-05 增加全检与自动扣量组合。用户明确允许两项且原任务已有全检规则时，可在同一个父检验计划中提交。扣量依据沿用测试配置，组合要求剩余数量为正整数。

原生逻辑先更新检验计数，再扣量，随后按 MES 失败状态处理失败转全检规则。因此，若原任务为失败转全检且尚未处于全检模式，条件要求数使用扣量后的数量。已处于全检模式的任务保持原要求数。预览同时展示原要求、预计剩余量及条件要求数，原任务字段仍由 MES 处理。配置或数量变化拒绝原计划，提交结果未知时仍检查原任务及父子历史。归零与组合分支现场效果待验收。

本次 504 项相关测试通过，全项目 4607 项测试及 2 项子测试通过，真实 HTTPX 网络尝试为 0。确认页面最终调整后另跑 11 项展示测试通过，覆盖两个组合效果和归零关闭许可。现场只读查询确认当前没有配置自动扣量的测试或全检任务。回归和现场记录位于本地 `docs/quality-fixtures/quality-effects-full-junit.xml`、`quality-effects-full-network.json` 与 `quality-effects-live-target-check.json`。

2026-11-05 扩展自动扣量归零。原生自动扣量已获明确许可时，可另选 `allow_zero_quantity_loss=true`。拟扣量须恰好等于当前余额，原生子服务必须明确返回 `CloseWhenZero=true`，预览展示 `projected_close=true`。正式请求保留检验输入，归零开关留在本地计划。归零后的实际关闭和任务状态通过原对象检查及最终历史审计核实。355 项相关离线测试通过，真实 HTTPX 网络尝试为 0；现场归零尚未验收。

2026-10-28，扩展现有 `prepare_container_inspection`，质量工具数量仍为50。新增两个默认false的显式选项。后续组合支持见本文最新记录。本阶段是受限首版，真实转换、内部扣量、签名及最终父子历史尚未验收。[高级功能计划](quality-advanced-roadmap.md)

| 参数 | 已开放范围 |
|---|---|
| allow_full_inspection=true | 按MES当前任务中已有的全检/失败转全检规则提交检验，支持满足当前全量要求的失败任务续检。不直接修改要求数或标志，不是任意强制转全检 |
| allow_automatic_quantity_loss=true | 只配置 DecreaseBySampleSizeReason 时，按本次数值测量样本行数或计数样本合计扣量。只配置 DecreaseByRejectCountReason 时，按本次计数检验的正数拒收合计扣量。计数支持单组或明确分组。两种模式均由父检验事务内部触发。 |

默认行为继续拒绝全检、自动扣量和自动事件配置。只有用户明确允许对应原生处理时，代理才传true，不能为了绕过前置错误而自行打开。两个选项可同时明确为true，按原生顺序提交组合效果；组合要求扣量后保留正整数余额。

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

测量按拒收数扣量、多组逐原因报废、两种扣量原因并存及扣至负数仍拒绝。扣至零需独立归零许可，全检与扣量组合须保留正整数余额。原生分支分别读取 SamplesTested、FailedSamples 或首行原因子明细。

父检验实际执行只提交一次CollectSamplingData。逐点数据及移站false保持原接口格式；**不会另发ChangeQty来补扣量**。预览中的ChangeQty调用全部是execute=false，仅核对子服务的类型、原因、目标、数量和默认值。

子服务必须回显LossDetails类型4030、方向-1、CountsAgainstProduction=true、RecordAllQty=false，并与本次样本数及原步骤一致。原生CloseWhenZero、AdjustThruput、GenerateRollups、DisassociateWhenClosed和RecordAllQtySummaryOnly默认值会展示、绑定进计划；不会把单独数量损失工具的关闭/生产数量选项偷偷传给检验子事务。默认保持正数余额。明确allow_zero_quantity_loss=true后，可准备恰好扣至零的计划，须原生CloseWhenZero=true，并展示关闭投影。

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


## 计数按本次样本数扣量（2026-11-04）

本轮复用allow_automatic_quantity_loss选项，接入单组计数样本扣量。明确samples_collected和rejects，存在拒收时仍须明确已配置的拒收原因。只配置DecreaseBySampleSizeReason，主单位、基础容器、原任务/配置复读、原生子服务默认值、正数剩余量及互斥规则沿用现有实现。

标准CollectSampling_ChangeQty在SampleType=1时采用根SamplesTested作为损失量。本轮将明确的本次samples_collected同步到根samplesTested，并要求父预览完整回显该增量和原持久任务ID。原任务CompletedSamples仍独立通过SQL前后复读。样本数3、拒收数1时，投影损失量为3。counted_groups保持受限，单组使用根级计数参数。

计数载荷沿用既有样本明细和拒收原因子明细，实际执行仍为一次CollectSamplingData。unknown保留占用，inspect返回计数扣量模式和原容器/原任务证据，最终父子历史及实际数量影响仍需人工审计。

## C5 单组计数按本次拒收数扣量

使用 `prepare_container_inspection` 并明确允许自动扣量。当前 SampleTest 必须只配置 `DecreaseByRejectCountReason`。提交明确的本次 `samples_collected`、正数 `rejects` 和有效的 `reject_reason_name`。例如已累计拒收 1 件，本次检 3 件、拒收 2 件，预览按 2 件计算损失，初始库存 10 件时预计剩 8 件。

标准 `CollectSampling_CountedSample` 将本次拒收子行的数值累加到父事务 `FailedSamples`。`CollectSampling_ChangeQty` 将该值作为原生 LossDetails 的扣量，使用测试定义中配置的损失原因。样本增量仍通过 `SamplesTested` 传入。计划分别校验样本数、拒收子行、当前工序损失原因、主单位及预计余额。正式执行为一次 `CollectSamplingData`，`ChangeQty` 仅用于非执行预览。

开发验证通过 401 项相关回归，禁用真实 HTTPX 传输期间网络尝试为 0。现场只读查询未发现配置 `DecreaseByRejectCountReason` 的测试定义，本次未提交现场扣量。实际库存影响和最终父子事务历史待隔离验收。

## C5 分组计数按合计自动扣量

单容器可将 `counted_groups` 与 `allow_automatic_quantity_loss=true` 一起使用。各组须明确互不重叠的样本数和拒收数。每个正数拒收组提供一个有效原因，同一原因不可重复，零拒收组最多一个。分组数上限为 20，样本合计上限为 100，同时受原任务剩余数和当前库存约束。

例如 3 组分别检验 2、3、1 件，分别拒收 1、2、0 件。计数样本合计为 6，拒收合计为 3。按样本数配置预计扣 6 件，按拒收数配置预计扣 3 件，使用各自测试配置中的损失原因。分组中的拒收原因保留在检验明细里。确认页面显示组数、两个合计、扣量依据、主单位和预计剩余量。

根 `SamplesTested` 写入本次样本合计，原生逐组明细累加 `FailedSamples`。客户端保留原顺序和完整分组，校验每行回显和总量。执行前重建并核对同一计划，正式仍只提交一次 `CollectSamplingData`。结果未明时，核查入口返回原容器、原任务、计划扣量和分组合计，事务占用保留到人工核实。

现场非执行探测回显了 `SamplesTested=10` 及两条完整分组，包含拒收原因子行。该探测未绑定实际容器。已通过 451 项相关回归，禁用真实 HTTPX 传输期间网络尝试为 0。实际分组累计、库存扣量和最终父子历史待隔离验收。

本轮相关回归354项通过，新增计数扣量专项18项，离线HTTPX真实传输尝试0。现场只读Swagger成功确认samplesTested为可写int32字段。真实计数预览、实际内部扣量、生产数量及最终父子历史尚未验收。原生依据和本轮结果见本地 `counted-loss-native-*.json`、`counted-loss-live-contract.json`、`counted-loss-junit.xml`、`counted-loss-network.json`。
