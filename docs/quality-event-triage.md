# 质量事件首次分流：受限首版

2026-10-29。新增 `get_quality_event_triage_context`、`prepare_quality_event_triage`，该阶段quality分组为57个工具；后续新增[事件文字资料维护](quality-event-update.md)，当前共66个工具。这是通用质量事件流程的第一步：基础Event首次分流确认，可明确调整优先级，保留已有类别、责任人、角色和CAR关闭策略。

本轮只交付代码、参数、非执行预览约束和离线验证；现场元数据读取未成功，真实非空事件、预览、提交及最终历史仍待验收。没有向MES写入业务数据、发送通知或重启服务。通用事件创建/更新/开启/关闭、重新分派、类别升级、审批和电子签名尚未因此开放。

## 参数与顺序

| 工具 | 用途 |
|---|---|
| get_quality_event_triage_context | 核对基础Event、组织与分流配置，并调用原生execute=false预览；不保存本地可执行计划 |
| prepare_quality_event_triage | 准备同样受限的TriageQualityObject计划，保存不可变快照后由现有确认执行器单次提交 |

两个工具都必填 `event_name`、`organization_name`，可用 `event_instance_id` 固定原事件身份。准备工具还接受：

- `priority_level_name`：只有调用方明确要求调整时提供；省略/null保留原优先级，空字符串不用于清空。
- `comments`：最多200字符的备注，追加在本轮唯一MCP关联标记之后。

没有owner、role、category、流程模板、检查表、签名口令或原始动作位透传参数。只有真实基础Event表记录，且CDOTypeId=7489；不根据名称前缀或QualityObject视图类型猜对象身份。

1. 先用已有质量对象详情/历史取得真实目标和组织；需要时固定原事件ID。
2. `get_quality_event_triage_context`检查首次分流前置及原生非执行回显。返回完整不表示已分流、批准或完成签名，也没有可供执行的plan_id。
3. 明确是否调整优先级，调用 `prepare_quality_event_triage`，审阅原事件、保持的归属/类别/CAR策略和目标优先级。
4. 系统确认后，`execute_quality_transaction(plan_id,plan_digest)`重建快照并检查占用，只发送一次原生TriageQualityObject请求。HTTP不重试、不跟随重定向。
5. 提交后先unknown。`inspect_quality_transaction`只读原ID事件和最多20条已有质量历史，不因TriageComplete=true、优先级相同或HTTP200就自动成功/解锁。最终事务证据通过 `review_quality_transaction`人工审核。

## 首版范围

原事件必须已提交、Active=1、未冻结、TriageComplete=false，已有明确的OwnerId、RoleId和PriorityLevelId。现有ProcessModel、OrigProcessModelTemplate、Checklist、已保存检查表、关闭信息或任何直接审批表都不属于本入口范围。

组织、责任人、角色、原/目标优先级和分流定义必须可按持久ID核实，名称与ChangeCount有效。读取Employee时只选择身份/冻结字段，不请求或持久保存该建模对象的凭据字段。

配置未冻结是本入口的保守范围，不是对所有MES冻结配置运行语义的断言；不自动解冻配置以使准备通过。

组织的CategoryMap按当前事件类别必须有唯一有效映射；所关联TriageSpec中，保持当前类别的ToCategory明细也必须唯一。读取完整清单，分别最多100项，重复、缺失、未知标志或溢出都拒绝，不按首行推定默认规则。

所选分流明细必须满足：

- 无ProcessModel/ChecklistTemplate，AutoRoute=false。
- CARRequired与原事件IsCARRequiredToClose相同。
- Owner/Role默认为空或与原事件完全相同；不能用该入口覆盖新的分派要求。

保持原类别，所以不调用类别升级，也不借同类别分流去改变责任人或角色。输入保留原Owner/Role，原生比较新旧Owner时相同，标准首次分流路径不请求所有权变更通知。部署自定义扩展仍未核实，不能把非执行预览当作全部副作用已验收。

## 动作、权限与签名

原生QualityRecordActions枚举中Triage=16。预览必须完整返回AvailableActions，且包含该位；只检查原码，不让调用方设置AvailableActions/ActionMask。

QualityObjectDetail必须与原事件的组织、类别、状态、责任人、角色、原优先级和CAR策略一致，且：

- UserHasRole=true、UserIsOwner=true，当前未分流。
- IsApprovalRequired=false，CompletionApprovalSheet、EntryForApproval及ProcessModel为空，CompletionApprovalStatus为空。
- 原生IgnoreProcessModel=false、RouteProcessModel=false，不由请求关闭或绕过流程检查。
- ProcessModelTemplate、ChecklistTemplate为空；TriageSpecDetail及其父级ID与已核实配置一致。

组织CollectESigForAllQualityTxns=true，或完整QualityESigTxns清单包含7788（TriageQualityObject）时拒绝。未知/重复签名清单、非空或缺失的ESigRequirement/AdditionalESigReqs也拒绝。不收集或保存签名口令，不把系统确认当作审批或电子签名。当前账号标志和动作位不是最终动态权限验证；原生执行仍负责校验Employee/Role/Organization组合及业务条件。

请求只包含原事件/组织、保持的类别/责任人/角色/CAR策略、明确或保留的优先级、TriageComplete=true和关联备注。不会填IgnoreProcessModel=true，也不下发流程、检查表、签名或动作位覆盖。

## 状态变化与未知事务

准备预览后及执行前重读原事件、参考配置、组织类别映射、分流明细、签名设置、直接审批表和关联NCR；变化则拒绝提交。已完成首次分流的事件不能用本工具重分流或把标志复位false。

最多读取20条引用原事件的NCR，核对完整原事件/容器ID及变更计数。当前质量事务库中，对原事件及这些关联容器声明占用，避免与未知NCR升级/收尾交叉执行。其他独立事务库或外部MES客户端不受此本地锁统一控制；多次重读也不是跨请求原子快照。

unknown检查固定原事件ID：已更名时用当前名称和原ID读有限历史，原ID消失时报告缺失，不追随同名新事件。当前分流标志、相同优先级、空历史或HTTP错误都不证明本次事务已提交/未提交，不允许自动重试或清库解锁。

## 原生依据

- [现场TriageQualityObject Swagger归档](quality-fixtures/contracts.json)中，事务定义7788提供Category、Owner/Role、PriorityLevel、TriageComplete和流程相关字段；不是简单的归属更新API。
- [字段目录](quality-fixtures/event-triage-native-fields.json)、[默认值](quality-fixtures/event-triage-native-defaults.json)：TriageComplete默认1，Owner/Role可能从TriageSpecDetail派生，所以必须明确保持原值并核对实际回显。
- [原生执行CLF](quality-fixtures/event-triage-native-execution.json)：变更Owner会调用组织SendEventEMail；存在模板时可能分配检查表、创建/指定流程；本工具不开放这些组合。
- [动作和状态元数据](quality-fixtures/event-triage-native-actions.json)：Triage位16，QualityObjectStatusDetail提供当前角色/责任人及审批上下文。
- [类别映射与权限逻辑](quality-fixtures/event-triage-native-links.json)、[原类别表与基础Event定义](quality-fixtures/event-triage-native-category-map.json)：GetTriageSpec读取Organization.CategoryMap；Event类型7489；ApprovalSheets按Parent指向原事件。
- [固定列目录](quality-fixtures/event-triage-native-columns.json)及[既有NCR列证据](quality-fixtures/table-columns.json)：Role实体实际表为RoleDef，分流明细父列为TriageSpecId。运行时仍要求当前dbo必要字段全部可核实。

标准MDB SHA256为`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。它不是部署自定义一致性的证明。[本轮只读检查](quality-fixtures/event-triage-live-readcheck.json)没有完成现场元数据读取，不能据此判断表为空或当前接口健康。

## 验证及后续

测试覆盖首次分流状态、明确优先级/保持值、原生动作和账号标志、组织签名清单、默认流程/检查表/CAR/分派阻断、配置变化、审批、关联NCR占用、类型/权限字段覆盖、单次提交、超时/取消后的unknown、原ID检查、凭据字段不进入快照和MCP实际调用/确认。

本轮新增119项测试，专项404项通过；全量2771项及2项子测试单次全部通过，其中质量专项1565项通过。最终离线HTTPX传输尝试为0。

结果见 [本轮汇总](quality-fixtures/event-triage-summary.json)、[全量JUnit](quality-fixtures/event-triage-junit.xml)、[最终离线HTTPX尝试](quality-fixtures/event-triage-network.json)、[工具/参数与问法核对](quality-fixtures/event-triage-tool-manifest.json)。离线守卫不是操作系统级网络隔离，问法测试只验证确定性工具选择，不表示模型已正确完成现场业务。

下一步仍需开发/验收：真实非空事件的预览、提交和历史效果；更广的事件创建/更新/开启/关闭、明确重新分派与通知授权、类别升级、流程路由、审批及电子签名，按[高级功能计划](quality-advanced-roadmap.md)继续推进。
