# C2.C第一闭环：质量审批条目一次转派及受委托签批

更新日期：2026-10-30。本轮实现直接Event/CAPA当前指定审批人对单个条目的一次原生DelegateTask转派，以及新审批人批准/拒绝、按组织要求的单人质量签名、原周期历史与未知结果恢复。代码及隔离模拟不能代替现场验收；不将整个C2.C或质量模块标为完成。

## 原生语义与边界

标准DelegateTask8682继承GenericTxn，DelegationTarget可以是ApprovalSheetEntry。其StartModifyingEntity绑定DelegateTask_Execute，DelegateApprover把当前Approver存入服务SourceEmployee、把条目Approver设为TargetEmployee，并可能按Factory配置向原/新审批人发送ChangeMgtApprovalDelegation通知。DelegateTaskToDelegateTaskHistory持久保存原条目、SourceEmployee、TargetEmployee、原因、评论及TrackableObject作为HistoryId。

这是一项**当前条目的一次转派，没有开始/结束日期**。不能把用户的“仅代理到周五”“批量代理”“到期自动还原”转换为此操作。DelegationTask是另一种带日期的配置；当前标准ApplicationTypeEnum仅ChangeManagement1，所读ApprovalSheetEntry.Delegate绑定的CLF中还引用Collaborator字段，质量日期任务的适用范围和实际行为尚未证明。本轮保留该分支的阻断，不建立日期配置、不自动转发或撤销。

原生DelegateApprover未写AssignedApprover。MCP保留准备时原AssignedApprover及SourceEmployee；若AssignedApprover为空，依然用唯一当前周期持久转派历史记录原人员。SignApproval按当前Approver签批，原生SignApprovalHistory.Approver/ApprovedBy可能均为新人员，不能把它们解释成转派前人员；转派历史和签批历史应组合核对。

## 工具及完整多轮流程

当前73个质量工具：新增 `prepare_quality_approval_delegation`，复用执行、检查和人工审计工具；已有审批上下文与批准/拒绝入口增加可选参数。

1. “查看组织Y事件X的审批。” → `get_quality_approval_context`，取得原对象ID、表ID、条目ID、LastApprovalCycleGMT、当前人员/角色及原生当前审批入口。候选不代表授权。
2. 原因候选可先用 `list_quality_references(kind='delegation_reason')`，详情用 `get_quality_reference_details` 同kind；候选不是转派许可，不自动选首项。“将这个条目转给张工，委托原因是出差，说明为……，允许通知。” → `prepare_quality_approval_delegation(name, organization_name, approval_sheet_id, approval_entry_id, approval_cycle_gmt, target_employee_name, delegation_reason_name, comments, allow_notifications=true)`。CAPA明确object_kind='capa'，可绑定原对象instance_id。
3. 准备核对已提交、未冻结Active/InReview基础对象；无流程、事件数据或检查表；Routed表与当前周期匹配；当前员工入口为原条目；Named1、无本周期已有签批或转派链、无日期任务；新旧人员不同且都具备原角色/组织的原生授权。完整读取原因及版本；不自动代选人员/原因，不接收Employee/User/口令覆盖。
4. 当前DelegateTask Swagger必须核实TrackableObject/DelegationTarget的BaseObjectRef、TargetEmployee/DelegationReasonCode的NamedObjectRef及固定POST契约。准备仅execute=false回显；固定原对象及条目、目标、原因、说明，实际当前Employee必须等于原Approver。转派函数绑定在StartModifyingEntity，标准预执行用户逻辑没有该转派；实际部署自定义与execute=false行为仍须验收。签名要求非空/未知或ProcessESignatures不是true即拒绝，不能关闭处理来绕过。
5. 展示原/新员工、周期、原因、说明和可能通知的影响，通过 `execute_quality_transaction`系统确认单次DelegateTask；核验和提交的认证头/Cookie仅在调用内存绑定，全链路使用验证证书的HTTPS，不跟随重定向，不持久化认证信息或原生响应/异常正文。
6. 返回先unknown。`inspect_quality_transaction`读取原条目、原周期及DelegateTaskHistory/HistoryMainline候选；HTTP200、当前Approver变成新人员或同值历史均不自动证明本次请求成功。原对象/表与其它质量工具共享占用，**原转派未完成最终人工审计前，不允许实际签批或换工具绕过**。`review_quality_transaction`沿既有规则处理最终证据；不能自动填人工结论。
7. 新审批人通过自己的合法MES会话继续。“查看该条目的转派历史。” → `get_quality_approval_context(include_delegation_history=true, delegation_entry_id=原条目ID)`，省略条目时选原生当前员工入口；明确读取最多50条当前周期历史，超限/变化拒绝部分结果。不切换账号、不代填登录信息。
8. “批准/拒绝此受委托条目，说明……” → `prepare_quality_approval_decision(…, allow_delegated_approval=true, delegation_history_id=原生转派历史ID)`。要求唯一当前周期DelegateTask8682持久历史、精确原对象/条目、原发起人与原生Employee一致、未撤销（受限ReversalStatus0）、Source/Target不同、Target=当前Approver=本次原生Employee，原AssignedApprover为空或等于历史Source。新人员原角色/组织资格再次核实，Approve512、当前入口及层级/决策规则保持。不能同时allow_role_substitution。
9. 组织要求SignApproval单人QualityESigDetail时可组合 `require_quality_esign=true`，签署人固定新审批人，沿独立本机页面本人输入及一次性引用；原人员口令不能代替新人员签名。确认同一计划单次SignApproval，unknown不重试；保留原周期、原Source、当前Approver及实际ApprovedBy，不将Rejected说成整表立即退回。

## 历史、并发及异常保护

转派历史按DelegateTaskHistory.DelegationTargetId=原条目、HistoryId=原质量对象，并精确关联HistoryMainlineId/TxnId/HistoryId，TxnType8682、TxnDateGMT不早于原周期（GMT保留7位小数）。最多50项，去重和完整字段核对；不是把同名人员或只有当前Approver当委托依据。

本周期首次转派后禁止连续转委托，撤销/恢复原人需另行原生依据和闭环；不能直接改数据库或清掉DelegationTask/AssignedApprover。更早周期记录不授权当前周期。现有带委托Task指针的条目继续拒绝；角色替代、资料修订/重提及取消审批入口的门禁不因本功能而放宽。受委托拒绝后的复杂修订/新周期转派不在本轮闭环范围。

执行前重读完整对象、表/周期、条目、历史、原因、双方员工和原生角色组织函数/快照。变化即停止旧计划。400、超时、取消、执行后本地日志失败等不重试；日志失败返回无法确定并保留in_flight，须最终审计恢复。新建计划、换工具或重新输入签名均不能绕过unknown/in_flight占用。

本地锁不约束外部MES客户端；读取后到实际提交仍可能有外部并发，最终应由MES权限、事务锁和校验决定。没有现场非空事务证据前，不宣称部署并发或完整通知行为已验收。

## 原生依据与验证

标准MDB SHA256 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。见[类型及日期任务](quality-fixtures/approval-c2c-native-index.json)、[转派流程](quality-fixtures/approval-c2c-native-flows.json)、[服务字段及历史列](quality-fixtures/approval-c2c-delegate-raw.json)、[字段映射](quality-fixtures/approval-c2c-field-maps.json)、[条目方法绑定](quality-fixtures/approval-c2c-entry-bindings.json)、[预执行与执行边界](quality-fixtures/approval-c2c-execution-flows.json)。标准模型不是现场部署证明。

隔离模拟涵盖Event/CAPA转派→unknown保护→虚构人工最终审计→新员工批准/拒绝→原生历史，以及新员工质量签名。原生状态转换、历史及人工审计均为显式测试夹具，只写临时计划库；不是实际Camstar引擎运行或真实业务审批。

证据：[交付汇总](quality-fixtures/approval-c2c-summary.json)、[完整回归](quality-fixtures/approval-c2c-full-junit.xml)、[最终定向](quality-fixtures/approval-c2c-final-junit.xml)、[工具/多轮问法](quality-fixtures/approval-c2c-manifest.json)。HTTPX离线守卫不是操作系统级隔离。本轮未真实转派、批准/拒绝、发通知、切换账号、修改服务/MDB、部署或重启现用服务。现场连接及实际契约/权限/通知/历史验收继续由C0承接；多签接口阻塞保持原状态。
