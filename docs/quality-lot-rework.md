# 受限整批返工

2026-10-28 新增 `prepare_sampling_lot_rework`。正式执行复用 `execute_quality_transaction`，只提交一次结果为 2 的 `ExecuteSamplingLot`。逐成员 `Rework` 请求用于非执行预览。2026-11-04 已核对隔离批 LR 的返工路线、数量、任务清理和最终父子历史，以及 LF 冻结成员失败后的状态和历史。[隔离验收记录](c4-isolated-acceptance.md)

## 参数与执行流程

| 参数 | 要求 |
|---|---|
| lot_name | 明确存在的SamplingLot名称 |
| rework_reason_name | 用户明确选择的已配置返工原因，必须属于每个成员当前Operation的完整ReworkReasons组 |
| lot_instance_id | 可选；同名歧义时固定16位十六进制ID |
| comments | 可选；最多180个UTF-16单元，另附事务关联标记 |
| expected_path_name | 多路径时必填，明确核对所有成员的首条原生默认路径名称；单路径时可选 |

不接收成员子集、目标Spec/步骤覆盖、数量覆盖、报废原因或任意结果码。原因候选可先用 `list_quality_references(kind="rework_reason")`，不自动选第一条。

1. 查询批详情、成员和检验任务，明确返工原因。
2. 调用 `prepare_sampling_lot_rework`。准备只读MES及非执行探测，保存本地不可变计划。
3. 展示完整成员、原数量、预计目标/返回步骤、原因和原生清理影响。预计路线来自静态配置，不等于运行时已解析。
4. 用户审阅后，由统一执行工具的确认流程确认同一 `plan_id/plan_digest`；执行前完整重建摘要，配置、成员或状态变化均阻止提交。
5. 单次正式提交后结果先为 `unknown`，保留原批、原成员和配置占用。`inspect_quality_transaction`读取原批和原成员当前记录，不跟随替换成员，不把当前返工结果或HTTP200当成功。
6. `reconcile_quality_execution` 按原标记核对父批结果及逐成员移动历史，`find_quality_execution_candidates` 可列出候选。历史匹配后可经确认用 `review_quality_transaction` 的 `resolve_history` 复读原成员当前路线、数量和任务清理后结案。证据不足时使用人工审计。详见[处置历史归因](quality-disposition-history.md)。

## 第一阶段限制

- 原批Active、尚无处置结果，MES SamplingPassed明确false，完成标志必须明确；有实际任务Rejects超过AllowedRejects的证据，继承/派生计数一致。允许失败证据在全部样本完成之前出现，不据此伪造整批质量结论。
- 全量1..20成员，同产品版本，均Active、无冻结、正数量、无父子层级、无非零第二数量，且明确不在返工中。原生分支会过滤非Active/零数量成员，本工具提前拒绝这些批，避免静默遗漏。
- 当前Workflow内有完整返工路径且没有动态返工选择器。多路径时必须明确expected_path_name核对原生首条路径，子服务预览完整回显路径、目标和返回步骤。跨Workflow、非Spec步骤、额外ReturnToStep配置仍受限。
- 当前Spec与步骤及当前Operation一致；源/目标Workflow与Spec固定版本均Active，目标Spec明确AllowReworkTo=true。原因必须同时适用于所有成员。
- 单Workflow最多200步骤、400条持久路径；单步骤最多50返工路径/选择器，原因组最多100项、配置依赖最多100个。超过上限或分页不完整则拒绝，不用部分集合准备整批操作。
- 不处理转全检、自动扣量、批报废或复杂审批签名。原生数量处理沿用服务默认；即使非执行回显MoveAllQty=false，也不覆盖Qty或承诺全量移动/扣减。

## 核实的原生行为及影响

现场OpenAPI/EDM确认 `ExecuteSamplingLot` 发布了结果1/2/3；这不是三个分支均可用的证明。`ScrapReason`仍标注当前InSite版本未实现，本轮不开放结果3。[批服务契约](quality-fixtures/lot-rework-schema.json)、[EDM与探测](quality-fixtures/lot-rework-contracts.json)、[子服务契约](quality-fixtures/lot-rework-child-schema.json)

使用官方Designer元数据组件只读核对了 `server_snapshot/Base_InSite.mdb` 的标准csi模型，源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。这是标准快照，**不是部署自定义覆盖已核实的证明**。

| 原生定义 | 核实结果及工具处理 |
|---|---|
| ExecuteSamplingLot_StartModifyingEntity | 逐Active且Qty>0成员，结果2及原因非空时执行原生返工；最后设置批SamplingCompleted=true及结果2 |
| ExecuteSamplingLot_ExecuteRework | 创建Rework子服务、映射字段、设置成员Container、执行，然后清理成员ContainerSampleData |
| ExecuteSamplingLotToRework | 仅映射Employee、Factory、ReworkReason、TxnDate、TxnDateGMT、User；不转发Spec、目标步骤、Qty、MoveAllQty或根Comments，因此不提供实际不会生效的目标工序参数，不假设根关联评论出现在每条成员历史 |
| Step_GetReworkPathToUse | 先评估ReworkPathSelectors，无选择器命中则返回ReworkPaths首项；描述说明单路径默认。工具接受单路径默认；多路径须明确核对首项并完整回显路由，均要求无选择器。不存在假定的SpecStep.DefaultReworkPath字段，不用普通DefaultPath代替 |
| Rework_ValidateReworkReason | 校验成员当前Operation.ReworkReasons.ResolvedEntries，不是Spec上的自造原因组 |
| Rework_ValidatePath | 要求ReworkPath，不能把普通移动路径当返工路径 |

证据：[批逻辑](quality-fixtures/lot-rework-clf-definitions.json)、[字段映射](quality-fixtures/lot-rework-map-rows.json)、[单路径默认规则](quality-fixtures/lot-rework-single-path-clf.json)、[默认字段核查](quality-fixtures/lot-rework-default-path-metadata.json)、[校验逻辑](quality-fixtures/lot-rework-native-validations.json)、[移动逻辑](quality-fixtures/lot-rework-native-movement.json)。

**预览明确展示：标准逻辑会删除成员ContainerSampleData，并标记批完成及返工结果。**没有据此声称清空SamplingLot归属、仅修改批头或已经核实扣量、移站、计时器、事件、签名等连带影响。各项验证标志保持false，实际效果须现场验收。

## 分层读取与防变化

现场服务器只在展开叶层返回完整字段，展开路径时步骤标量会置空，Changes-CDO标识也可能临时分配。工具分别读取Steps叶字段和返工路径/选择器，按同一Workflow版本内唯一名称关联；固定SQL读取WorkflowStep、Path的持久ID、所属关系、目标及变更计数，再核对建模引用。不把临时标识当实际步骤，配置摘要排除明确Changes引用的临时ID。

`Rework(execute=false)`必须回显成员和原因；可能没有Path或目标步骤。已返回的路线不得与配置矛盾，缺失保留未知，`runtime_route_resolution_verified=false`。原批、完整成员/任务、原成员运行状态、模型配置与变更计数准备后重读，执行前再次重建。跨请求核对不是MES事务快照，不是全局锁。

现场只读验证了真实Workflow的分层读取及重读一致；示例Workflow没有返工路径，不能算正目标验收。无目标整批非执行探测只回显结果2，不能据此创建ready计划。[原始展开](quality-fixtures/lot-rework-workflow-expansions.json)、[分层身份核对](quality-fixtures/lot-rework-layered-readcheck.json)、[无目标探测](quality-fixtures/lot-rework-readonly-smoke.json)、[持久路径列](quality-fixtures/lot-rework-path-runtime-schema.json)

## 测试与交付状态

新增96项模拟测试；全量2183项、2项子测试通过，质量相关977项，父pytest离线HTTPX真实网络尝试0。测试覆盖MCP参数封闭、准备/确认/单次执行、完整成员、原因组、单路径与目标许可、分层字段缺失与临时ID、配置变化、严格请求字段、unknown防重及原成员检查。最新全量结果见[测试输出](quality-fixtures/lot-rework-test-output.txt)、[JUnit](quality-fixtures/lot-rework-junit.xml)、[离线网络记录](quality-fixtures/lot-rework-network.json)、[阶段摘要](quality-fixtures/lot-rework-summary.json)。

现场正目标的完整准备、实际返工与数量/历史/后续影响、部署自定义覆盖及真实LLM多轮选工具仍待验收。本次没有MES业务写入、清理事务库、部署或重启服务；本地新进程注册不代表运行服务已加载新增工具。


## C4第二闭环：明确多路径默认返工

2026-11-04。已有原生整批返工工具增加可选expected_path_name。所有成员当前步骤的首条返工路径名称都必须等于该值，支持多个已配置返工路径且无动态选择器的流程。参数用于核对默认路径，整批请求仍只包含原批、结果2、返工原因和评论。

每个成员保留完整有序路径清单和持久ID，非执行子服务必须回显Path、ToWorkflow、ToStep、ReEntryWorkflow、ReEntryStep，且与配置一致。完整依赖及成员在准备后复读，执行前重建摘要。首项或其它路径配置变化、成员变化、回显缺失或矛盾都会拒绝。单路径旧调用保持兼容。

原生批事务仍会清理ContainerSampleData并标记批完成及返工结果，实际提交保持一次ExecuteSamplingLot。unknown保留原批/成员占用并等待最终审计。路线解析及最终数量/历史效果仍待现场验收。

本轮相关回归315项通过，其中新增多路径专项17项，离线HTTPX真实传输尝试0。现场只读Swagger成功确认ExecuteSamplingLot没有path覆盖入口。标准Step_GetReworkPathToUse与ExecuteSamplingLot_ExecuteRework已只读复核，证据保存在本地c4-rework-native-*.json及c4-execution-live-contract.json，测试报告为c4-rework-paths-junit.xml/c4-rework-paths-network.json。整批报废的原因类型兼容及内部全量扣量/原子性仍待核实。


## C4第三闭环：成员采样任务清理基线与返工后证据

准备批返工时固定所有原成员的ContainerSampleData任务，最多200条。保存每条原任务的持久ID、容器/测试/计划ID、采样/拒收计数及变更版本。准备前后复读，执行重建计划时继续核验，新增任务、已有结果或版本变化会使旧计划失效。预览给出将受清理影响的原任务数量，完整基线保存在不可变计划内。

inspect按原成员ID读取当前任务，分别报告原任务缺失、原任务仍在、原任务内容变化和新增任务。旧任务集合为空时，清理结果保持未证明。旧快照没有清理基线时返回baseline_available=false，原任务数为未知；升级前待执行计划需要重新准备以绑定新基线，已尝试计划继续按原快照读取证据。

另核对原成员当前Workflow/Step/Spec、InRework标志及数量，逐成员比较预期返工目标。记录存在但路线查询暂不可读时，保留原记录并明确标记路线不可用。原成员记录、任务及可读路线均复读，变化时拒绝不一致证据。任务消失和当前去向相符作为审计材料，最终事务因果关联继续由人工核实。

标准ExecuteSamplingLotHistory与HistoryMainline列目录已只读探索。成员ReworkHistory没有独立同名物理表，本轮未假设父子历史键，自动成功归因仍未实现。整批报废原因类型兼容、内部全量扣量/关闭与原子性继续列为C4未完成项。

本轮相关回归330项通过，其中新增专项15项，离线HTTPX真实传输尝试0。本轮没有MES业务写入、部署或重启，真实数量、采样任务清理和最终事务历史仍待现场验收。本地结果为c4-rework-evidence-junit.xml和c4-rework-evidence-network.json。
