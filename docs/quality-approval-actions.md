# 质量审批表首次路由与未签批取消

2026-10-29新增 `prepare_quality_approval_action`，该阶段67个质量工具；后续新增[审批决策计划](quality-approval-decisions.md)，当前68个工具。新增route/cancel的实际计划与统一确认执行；本轮没有真实审批、通知或服务重启，实际签批、退回和电子签名仍未完成。

## 参数与流程

必填action（route/cancel）、name、organization_name、approval_sheet_id及非空comments（最多200个UTF-16代码单元）；object_kind为event（默认）或capa，可绑定instance_id，allow_notifications默认false。

先get_quality_approval_context只读核对明确原对象与表。准备时固定原表ID、父对象、完整条目及决策列表/周期，查询当前专用表级服务Swagger并做execute=false精确回显。展示原状态、审批人/角色ID、当前账号、预期原生效果及通知策略，通过execute_quality_transaction系统确认后，只提交一次RouteApprovalSheet或CancelApprovalSheet。

不接受任意服务名、原始body、审批状态覆盖、签署口令或decision。不是批准/拒绝/退回决策入口；普通确认不能当作签名，不用通用RouteApproval或CancelApproval代替表级服务。

## 受限范围

原Event/CAPA必须未冻结、已提交，处于Active或InReview。Event无EventData、检查表或流程/原流程模板；CAPA无流程上下文。表及所有条目未冻结，原审批人/角色必须明确，决策列表存在、未冻结且有决策。

route只支持Pending0表首次路由，LastApprovalCycle和GMT均未设置；条目全部Pending，DecisionType=0，无既有决策、已批准人、LastDecision或完成时间。原生Sheet.Route也会ClearApprovals，所以不能用新入口重路由或清空旧周期，即使旧表已回Pending也拒绝。

cancel只支持Routed10表，条目仅Pending/Routed且均未签批。任何已有签批、决策或完成历史都会拒绝，不取消/覆盖已签结论。原生CancelApprovalSheet将表回Pending0，对尚无决策条目写RoutingCanceled历史并将质量对象设Active；**不是将审批表设成Cancelled20**，也不是审批拒绝/退回。

原账号须为原责任人且具原角色；cancel还必须有原生CancelApproval动作位256。route没有在已确认枚举中独立权限位，不编造Route位；原生最终检查审批人、角色、权限和流程约束。预览或有效引用不表示业务批准。

组织签名要求按表级服务RouteApprovalSheet7838或CancelApprovalSheet7837核实，原生ESigRequirement/AdditionalESigReqs必须明确为空。不接收口令，也不修改签名标志。它们与通用RouteApproval8567、CancelApproval8566是不同服务，不混淆事务码。

## 通知与防重

读取组织受限SMTP/通知策略。可能通知时必须明确allow_notifications=true；原生通知可能发送到审批人及角色替代成员，不把已有SMTP配置等同为通知验收。工具不单独发送邮件、不展开SMTP凭据，也不清空通知配置。

准备后和执行前重建都重读原对象、所有直接表、周期/条目/决策、原人员/角色及通知配置；变化则停止提交。原对象与原审批表共同占用，并与事件维护、关闭、分派、CAPA维护和组织事件创建互斥。跨系统或未接入同一日志的其他程序不受该占用保证。

执行后先unknown。inspect仅按原对象ID和原表ID读取当前证据，原表不存在不会跟随同名新表。当前Pending/Routed、相同周期或HTTP200都不证明本次成功；最终事务历史需人工review，不自动重试或解锁。

## 原生依据

[动作服务目录与校验](quality-fixtures/approval-actions-native.json)、[审批表引用字段](quality-fixtures/approval-sheet-actions-fields.json)、[路由/取消/通知执行逻辑](quality-fixtures/approval-sheet-actions-native-flow.json)来自只读标准csi模型。ApprovalSheet为NamedDataSubentityNoncacheable；运行时仍要求当前Swagger的NamedSubentityRef模型，输入包含表名、原ID及父对象引用，缺失/改变契约拒绝准备。

RouteApprovalSheet.StartModifyingEntity调用ApprovalSheet.Route；Route依次Validate、ClearApprovals、Parent.Review、AutoRouteDetails并更新周期。CancelApprovalSheet.StartModifyingEntity调用Sheet.Cancel并置QualityObject.Active；Cancel写RoutingCanceled历史并调用Parent.CancelApproval。原生邮件可包含角色成员，工具不推算实际收件人或代发通知。

标准MDB SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。标准模型不是当前部署自定义一致性证明，离线Swagger明确是依据原生引用类型构造的测试响应，不能冒充现场契约。

## 验证及未完成项

结果见[汇总](quality-fixtures/approval-actions-summary.json)、[针对性JUnit](quality-fixtures/approval-actions-focused-junit.xml)、[完整JUnit](quality-fixtures/approval-actions-junit.xml)、[离线HTTPX守卫](quality-fixtures/approval-actions-network.json)。测试使用模拟接口和临时计划库；守卫不是操作系统级网络隔离。已有审批上下文仍只读，见[上下文说明](quality-approvals.md)。

本轮未实际路由、取消审批、签名或发送通知。真实权限、父状态/审批周期变化、通知、历史和部署覆盖尚待验收。带既有签批/历史的取消或重路由、嵌套流程、自定义事件数据、签批/拒绝/退回及受控电子签名交互仍不在此首版范围。

本轮新增50项测试，针对性180项通过；最终全量3216项测试、2项子测试通过，其中质量相关2010项，父pytest真实HTTPX网络尝试0。[工具/参数与问法核对](quality-fixtures/approval-actions-manifest.json)确认67个质量工具均注册且可分发，输入模型封闭；实际质量计划库只读检查仍为2条cancelled，无新增现场执行。人员引用意外多返回的字段不会进入计划/预览，测试覆盖凭据字段过滤。

[现场只读检查](quality-fixtures/approval-actions-live-readcheck.json)未取得有效元数据或两个服务Swagger，没有选择真实审批表预览/提交，不把失败读取说成无数据或有权限。

后续已单独实现无额外电子签名要求的原指定审批人批准/拒绝计划，详见[审批决策](quality-approval-decisions.md)。本表级route/cancel工具不执行决策，仍不能用取消代替拒绝或退回。
