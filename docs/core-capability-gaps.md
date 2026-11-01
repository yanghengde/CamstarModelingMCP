# 核心能力缺口与支持边界

核查时间：2026-11-01T11:38:39.466069+08:00（Asia/Shanghai）。

本次仅 GET 现场 Swagger 目录和契约，没有执行业务事务或 execute=false 预览。以当前 MCP 注册清单和代码限制人工对照；Swagger HTTP 200 仅说明接口定义可读，不证明业务许可或现场效果。

## 范围与证据

核心生产/质量/建模能力的人工审阅清单；不是所有Swagger端点的全量覆盖率。

Modeling 586 个分组、Shopfloor 249 个分组、Query 61 个分组；针对核心缺口核对 76 份契约，76 份可读。目录分组数含基类、维护服务和重复路由，不是业务功能数，不能据此计算覆盖率。

- [机器可读盘点及契约证据](core-capability-gaps.json)：保留目录分组、HTTP状态、相对地址、方法、请求schema字段/只读字段和响应摘要哈希。
- `python scripts/audit_core_capability_gaps.py`：重新读取现场Swagger并更新本文及JSON；支持状态和优先级仍须随实现人工复核。
- P1：生产/质量闭环优先候选；P2：扩展能力候选。优先级为本次建议，未自动排期。

## 当前缺口总表

| 编号 | 核心能力 | 状态 | 优先级 | 已有能力 | 尚缺范围 |
|---|---|---|---|---|---|
| G01 | 独立工单下发 | 业务契约待确认 | P1 | 已有工单建模维护、开工和过程任务。 | 尚未确认带业务校验的独立下发事务；orderStatus/releaseDate字段维护不能当作下发。 |
| G02 | 吞吐量及整单报工 | 部分支持 | P1 | 已有MoveIn/MoveOut/MoveStd和工单进度查询。 | 未封装Thruput/MoveWithThruput专用闭环；良品、不良、工时及整单完工未形成已验证流程。 |
| G03 | 容器关闭与非标准移站 | 未接入 | P1 | 支持正常进出站、返工、基础容器追溯。 | 无Close/MoveNonStd专用工具；独立容器关闭/任意目标移站未开放（数量损失计划中的显式归零关闭选项另见G09）。 |
| G04 | 完整物料投料模式 | 部分支持 | P1 | 模式2批量来源和模式5数量投料已接入计划及核对。 | 模式1序列容器、3批号、4库存点仅意图预览；替代料、第二数量未形成执行闭环。 |
| G05 | 退料、替换与分配物料 | 未接入 | P1 | 已有需求查询和投料历史。 | ComponentRemove/Replace/Distribute无专用执行工具，不能用负数投料模拟退料。 |
| G06 | BOM维护 | 部分支持 | P1 | 支持BOM详情、产品物料需求和版本比较。 | 尚无专用BOM创建/明细维护/升版工具；产品引用维护不等于维护BOM明细。 |
| G07 | 设备主数据与资源生产事务 | 部分支持 | P1 | 支持资源/资源组读取和Spec资源上下文。 | 尚无专用设备主数据维护、激活、设置转换、资源吞吐和资源采集执行工具。 |
| G08 | 设备保养及维修闭环 | 未接入 | P2 | 资源上下文可供只读检查。 | 尚无保养请求、保养完成及维护状态专项工具；不是已支持的OEE/排程。 |
| G09 | 容器数量损失的复杂场景 | 部分支持 | P1 | 基础单容器部分损失/明确全量报废已有受限LossDetails计划。 | Batch、抽样批成员、层级、载具及双单位未开放；PartScrap不是生产容器报废。 |
| G10 | 多成员抽样批报废 | 执行门禁关闭 | P1 | 已有只读报废前置检查、整批通过和受限整批返工计划。 | ExecuteSamplingLot结果3尚未开放；原因类型和实际成员影响待核实。 |
| G11 | 高级抽样检验组合 | 部分支持 | P1 | 单容器可显式允许已有全检规则或测量按样本数自动扣量，二者互斥。 | 抽样批全检转换、计数/拒收自动扣量、扣至零及事件组合仍未开放；不以Swagger字段存在作为启用依据。 |
| G12 | 现场SPC引擎及自动处置 | 部分支持 | P1 | 已有SPC配置查询、本地计算、受限注释和明确报告的单点越限登记。 | 不认证现场引擎；区内运行、派生值、批量判定、排除/恢复及自动冻结/放行未开放。 |
| G13 | 日期委托与连续转委托 | 部分支持 | P2 | 当前指定审批人的单次DelegateTask及受委托人受限签批已实现。 | 日期自动委托、连续转委托、批量转派、撤销委托和委托配置维护未开放。 |
| G14 | 通用多签与协签 | 执行门禁关闭 | P1 | 已有单人质量签名页面及严格条件审批；NCR通用多签有条件接入代码。 | 保存的标准REST契约尚缺已验证的ESigPasswordCapture/原生认证映射，通用NCR多签执行门禁关闭。 |
| G15 | 质量事件完整维护与通用生命周期 | 部分支持 | P1 | 已有受限创建、Pending资料补齐/正式提交、文字/标准扩展资料修改、开启、分派和条件关闭。 | 既有草稿分类修改无核实写入路径；任意自定义EventData、复杂检查表/明细和通用对象生命周期尚未完整开放。 |
| G16 | 文档集维护与现场附件 | 部分支持 | P2 | 支持Spec文档关联查询及受限正式下载。 | 文档/文档集专用维护与AttachDocument/DetachDocument现场附件尚无工具；不能把下载当作附件维护。 |
| G17 | 标签打印与补打 | 未接入 | P2 | 已有容器和对象身份查询。 | 无标签打印/补打专用工具；不能声称开工后自动打印已完成。 |
| G18 | 生产计时器 | 未接入 | P2 | 已有工序和容器基础状态。 | 未接入暂停任务计时、停止计时和过程计时器专项查询。 |
| G19 | 生产发运 | 未接入 | P2 | 现有容器开工、流转及追溯。 | Ship专用事务未接入；工序完成不代表已发运。 |
| G20 | 单位及全局基础资料的扩展维护 | 部分支持 | P2 | UOM已有列表/详情/新建/修改；已有部分层级、编号、产线、类型工具。 | 无UOM删除及单位换算专项闭环；其他原因/状态/组织等资料多为查询，不代表全部可维护。 |
| G21 | 事务结果自动核对与现场验收 | 验收与自动核对待补 | P1 | 已有多类不可变计划、单次提交、历史证据查询和人工最终核对。 | 多类写入尚待非空现场验收，unknown不能仅凭同值回读认领成功；尚无统一自动对账解锁。 |
| G22 | 复杂容器拆合与层级关系 | 部分支持 | P2 | 已有受限拆分、合并、装箱/拆箱计划、基础关系和数量核对。 | 多层已有子容器装箱、跨产品/工序/单位合批及复杂双数量场景未形成通用执行能力；不会隐式全部合批。 |

## 每项契约与后续验证

### G01 独立工单下发

Swagger分组：`Modeling/MfgOrders`、`Shopfloor/MfgOrderTxn`。

代码依据：[mfgorders.py](../tools/mfgorders.py)、[mfgorder_tasks.py](../tools/mfgorder_tasks.py)。

下一步：结合现场下发页面查明实际原生调用和历史证据。

### G02 吞吐量及整单报工

Swagger分组：`Shopfloor/Thruput`、`Shopfloor/MoveWithThruput`、`Query/MfgOrderInquiry`。

代码依据：[container_moves.py](../tools/container_moves.py)、[order_progress.py](../tools/order_progress.py)。

下一步：先核实单次吞吐历史与工单汇总，再定义报工边界。

### G03 容器关闭与非标准移站

Swagger分组：`Shopfloor/Close`、`Shopfloor/MoveNonStd`。

代码依据：[container_moves.py](../tools/container_moves.py)、[tool_catalog.py](../core/tool_catalog.py)。

下一步：核实目标步骤、当前状态、关闭前置及回读历史。

### G04 完整物料投料模式

Swagger分组：`Shopfloor/ComponentIssue`、`Modeling/BOMs`。

代码依据：[component_issue.py](../tools/component_issue.py)、[component_issue_plans.py](../tools/component_issue_plans.py)、[component_issue_payload.py](../tools/component_issue_payload.py)。

下一步：分模式补齐来源/批次/库存/替代关系及历史核对。

### G05 退料、替换与分配物料

Swagger分组：`Shopfloor/ComponentRemove`、`Shopfloor/ComponentReplace`、`Shopfloor/ComponentDistribute`。

代码依据：[tool_catalog.py](../core/tool_catalog.py)、[component_issue.py](../tools/component_issue.py)。

下一步：核对原投料追溯、退料目的地、替换原因及数量余额。

### G06 BOM维护

Swagger分组：`Modeling/BOMs`、`Modeling/ResourceBOMs`。

代码依据：[boms.py](../tools/boms.py)、[tool_catalog.py](../core/tool_catalog.py)。

下一步：补明细身份、版本和替代料契约，明确变更影响。

### G07 设备主数据与资源生产事务

Swagger分组：`Modeling/Resources`、`Modeling/ResourceGroups`、`Shopfloor/ResourceActivation`、`Shopfloor/ResourceSetup`、`Shopfloor/ResourceSetupTransition`、`Shopfloor/ResourceThruput`、`Shopfloor/CollectResourceData`。

代码依据：[resources.py](../tools/resources.py)、[tool_catalog.py](../core/tool_catalog.py)。

下一步：先核实资源状态模型、当前设置及允许转换。

### G08 设备保养及维修闭环

Swagger分组：`Modeling/MaintenanceReqs`、`Shopfloor/CompleteMaintenance`、`Query/GetMaintenanceStatuses`。

代码依据：[resources.py](../tools/resources.py)、[tool_catalog.py](../core/tool_catalog.py)。

下一步：核实保养要求、状态与完成后历史。

### G09 容器数量损失的复杂场景

Swagger分组：`Shopfloor/ChangeQty`、`Shopfloor/PartScrap`。

代码依据：[quality_quantity_loss.py](../tools/quality_quantity_loss.py)、[production_basics.py](../tools/production_basics.py)。

下一步：逐类验证数量、关闭选项、原因及父子事务证据。

### G10 多成员抽样批报废

Swagger分组：`Shopfloor/ExecuteSamplingLot`、`Shopfloor/SamplingLotTxn`、`Modeling/ScrapReasons`、`Modeling/LossReasons`。

代码依据：[quality_lot_rework.py](../tools/quality_lot_rework.py)、[quality_quantity_loss.py](../tools/quality_quantity_loss.py)、[quality_lot_execution.py](../tools/quality_lot_execution.py)。

下一步：核实ScrapReason/LossReason原生映射，验证整批成员及历史副作用。

### G11 高级抽样检验组合

Swagger分组：`Shopfloor/CollectSamplingData`、`Shopfloor/CollectLotSamplingData`、`Modeling/SampleTests`、`Modeling/SamplingPlans`。

代码依据：[quality_inspection.py](../tools/quality_inspection.py)、[quality_inspection_effects.py](../tools/quality_inspection_effects.py)、[quality_lot_execution.py](../tools/quality_lot_execution.py)。

下一步：核验原生父子事务和业务副作用，再分场景开放。

### G12 现场SPC引擎及自动处置

Swagger分组：`Shopfloor/InlineSPCTxn`、`Shopfloor/SPCTxn`、`Shopfloor/RecordSPCViolation`、`Shopfloor/AddSPCAnnotation`、`Modeling/SPCRules`、`Modeling/SPCControls`。

代码依据：[quality_spc.py](../tools/quality_spc.py)、[quality_spc_violations.py](../tools/quality_spc_violations.py)、[quality_spc_annotations.py](../tools/quality_spc_annotations.py)。

下一步：先核实原始历史、引擎规则与通知/处置配置。

### G13 日期委托与连续转委托

Swagger分组：`Shopfloor/DelegateTask`、`Shopfloor/DelegateTasks`、`Query/DelegateDateInquiry`、`Query/DelegateTaskInquiry`。

代码依据：[quality_approval_delegation.py](../tools/quality_approval_delegation.py)、[quality_approval_decisions.py](../tools/quality_approval_decisions.py)。

下一步：区分任务转派与日期授权，核验委托链、周期及实际签署人。

### G14 通用多签与协签

Swagger分组：`Shopfloor/SignatureApproval`、`Shopfloor/SignApproval`、`Modeling/ESigRequirements`、`Modeling/ESigCosignReasons`、`Shopfloor/NCRUpdate`。

代码依据：[quality_native_signatures.py](../tools/quality_native_signatures.py)、[quality_signatures.py](../tools/quality_signatures.py)、[quality_approval_decisions.py](../tools/quality_approval_decisions.py)。

下一步：取得现场具体签名捕获及主签署/协签映射契约，再验收真实安全页面链路。

### G15 质量事件完整维护与通用生命周期

Swagger分组：`Shopfloor/UpdateEvent`、`Shopfloor/UpdateEventData`、`Shopfloor/UpdateEventChecklist`、`Shopfloor/UpdateEventDetails`、`Shopfloor/OpenQualityObject`、`Shopfloor/CloseQualityObject`。

代码依据：[quality_event_drafts.py](../tools/quality_event_drafts.py)、[quality_event_data.py](../tools/quality_event_data.py)、[quality_event_update.py](../tools/quality_event_update.py)、[quality_event_closure.py](../tools/quality_event_closure.py)。

下一步：分类字段须核实独立原生写入；复杂类型逐个建适配与状态验收。

### G16 文档集维护与现场附件

Swagger分组：`Modeling/Documents`、`Modeling/DocumentSets`、`Shopfloor/AttachDocument`、`Shopfloor/DetachDocument`。

代码依据：[spec_documents.py](../tools/spec_documents.py)、[document_files.py](../tools/document_files.py)。

下一步：核实文件存储、访问权限、附件历史及文档版本。

### G17 标签打印与补打

Swagger分组：`Shopfloor/PrintContainerLabel`、`Shopfloor/ReprintContainerLabel`、`Modeling/PrinterLabelDefinitions`。

代码依据：[tool_catalog.py](../core/tool_catalog.py)。

下一步：核实打印机、模板版本、补打原因和实际打印反馈。

### G18 生产计时器

Swagger分组：`Shopfloor/PauseTaskTimer`、`Shopfloor/StopTimer`、`Modeling/ProcessTimers`、`Query/ProcessTimerInquiry`。

代码依据：[tool_catalog.py](../core/tool_catalog.py)。

下一步：核实计时对象、状态和时间区间，不将页面耗时当作生产工时。

### G19 生产发运

Swagger分组：`Shopfloor/Ship`、`Modeling/ShipmentDestinations`。

代码依据：[tool_catalog.py](../core/tool_catalog.py)。

下一步：明确收发业务、目的地、数量、状态和发运历史。

### G20 单位及全局基础资料的扩展维护

Swagger分组：`Modeling/UOMs`、`Modeling/HoldReasons`、`Modeling/LossReasons`、`Modeling/OrderStatuses`。

代码依据：[uoms.py](../tools/uoms.py)、[quality.py](../tools/quality.py)、[production_basics.py](../tools/production_basics.py)。

下一步：按真实业务需求逐项补齐资料维护；换算能力另查具体契约，不能从单位名称推导。

### G21 事务结果自动核对与现场验收

Swagger分组：`Shopfloor/CollectData`、`Shopfloor/ComponentIssue`、`Shopfloor/ExecuteMfgOrdertask`。

代码依据：[collection_reconciliation.py](../tools/collection_reconciliation.py)、[component_issue_receipts.py](../tools/component_issue_receipts.py)、[mfgorder_task_plans.py](../tools/mfgorder_task_plans.py)。

下一步：为每类请求建立可靠事务关联与最终证据，再开放自动核对。

### G22 复杂容器拆合与层级关系

Swagger分组：`Shopfloor/Split`、`Shopfloor/Combine`、`Shopfloor/Associate`、`Shopfloor/Disassociate`。

代码依据：[container_transfers.py](../tools/container_transfers.py)、[container_transfer_plans.py](../tools/container_transfer_plans.py)、[container_split_plan.py](../tools/container_split_plan.py)。

下一步：按层级、主/第二数量和参与方范围分别验收，不扩大已确认计划。

## 对原能力介绍的修正

- 生产容器报废不能统称“未提供”：基础单容器已有受限数量损失/全量模式；复杂容器及抽样批报废仍受限。PartScrap是资源零件相关事务。
- 质量对象开启/关闭不能统称“未提供”：基础Event开启及条件关闭、CAPA关闭、NCR关闭已有入口；通用对象及复杂组合并未完整开放。
- 电子签名不是全部未支持：已有单人质量签名与条件审批；通用NCR多签/协签门禁仍关闭。
- UOM列表、详情、新建和修改已接入；维护单位名称不代表换算关系可用。
- “所有写入均需确认”已不准确：确认遵循当前对话的标准/自动模式和执行层策略；所需选择、缺失参数及用户明确要求先确认仍需等待。
- Swagger存在、工具已接入、仅预览、执行开放和现场已验收分别陈述，不将回读同值直接当作未知请求成功。

## 与历史资料的关系

[旧覆盖审查](mcp-tool-coverage-review.md)、[旧生产API盘点](production-api-capability-audit.md)保留为历史证据；其中未接入结论不能直接当作当前状态。当前缺口以本文及运行时注册目录共同核对；本表也不是未来版本能力承诺。
