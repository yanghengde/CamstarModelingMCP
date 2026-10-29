# Event/CAPA审批上下文只读核对

2026-10-29新增 `get_quality_approval_context`，当前质量工具66个。这是审批功能的读取与核对基础，不是路由、签批、退回、撤回或电子签名执行入口。

## 参数与使用

必填 `name`、`organization_name`，`object_kind`为event（默认）或capa，可提供 `instance_id`绑定原对象。示例问法：“查看事件审批层级”“查看CAPA允许的审批决策”“当前有哪些已路由条目”。对象名称和组织必须明确，不能猜审批对象或代填审批人。

结果显示原质量对象、组织策略、直接审批表、审批层级及条目、原审批人/角色/指定人ID、是否必签、代签配置、允许的决策原码及冻结、原生当前审批入口。表状态映射为Pending0、Routed10、Cancelled20、Rejected30、Approved40；未知状态明确Unknown，不猜成已批准。角色替代配置2不代表当前员工符合角色和组织条件。

`complete=true`仅表示本次限定读取完整并经重读一致；scope固定为直接质量对象审批表，不遍历或声称覆盖嵌套流程审批。`routed_candidate`仅表示表和条目都处于Routed且未冻结，不等于签批权限。原生EntryForApproval只作为当前入口证据，不能将其解释为已完成签名。

`ready_for_execution`、`actor_permission_verified`、`approval_action_performed`始终false，没有plan_id。不能将此报告交给execute_quality_transaction充当审批计划；普通确认不能替代审批或电子签名。工具不接受口令、签署身份覆盖、决策输入或任意服务名。

## 完整性与范围

固定元数据字段核实后读取原Event或CAPA，核对原ID、名称、组织、类别、状态、冻结及版本。组织签名要求只展示；需要签名不会阻止只读查看。共享配置读取函数仅在此只读调用使用不拒绝签名策略的模式，现有写入入口仍保留原签名阻断。

直接审批表最多20张，单表最多100条、本次总条目最多200条，每个决策列表最多50项。每张表/条目/决策的父级、ID、版本、冻结、原决策列表归属及唯一性必须核实；ID按十六进制规范化防止大小写重复。缺字段不补默认值，超过限额不返回部分清单；最终输出超过78000字符也拒绝部分结果。

允许决策来自原表绑定的ApprovalDecisionList，不使用通用的“同意/拒绝”默认码，不把名称相同的其他列表决策混入。IncludeComments保留原值，不猜测其等同“必须输入评论”。已冻结列表或决策仍展示冻结状态，不能视为可用许可。

独立QualityObjectInquiry核对类别、状态、组织；当前条目和完成审批表必须出现在本次完整直接清单中，否则可能为嵌套流程或读取变化，报告不完整，不猜父级。原对象、组织策略、审批表/周期/条目/决策在返回前再次读取；发生变化则拒绝旧上下文。多服务重读不是数据库事务级一致性保证。

## 签名与读取安全

只选择声明的审批业务列，不查询人员凭据、签名口令、签名原文或邮件内容；人员/角色以持久ID表示，不推断当前员工的权限。意外多返回的SQL列会被丢弃；Inquiry引用只保留稳定ID，不保存临时服务ID、完整人员对象或其他字段。

组织CollectESigForAllQualityTxns及QualityESigTxns用于显示SignApproval7839、RouteApproval8567的组织策略；不等于所有服务动态签名要求已核实，SignatureApproval等路径仍未开放。实际员工、角色、组织和替代签署资格仍需原生校验，不能由模型口头批准。

## 原生依据

[审批原生目录](quality-fixtures/approval-native-index.json)、[字段列和处理逻辑](quality-fixtures/approval-native-details.json)、[策略及代签枚举](quality-fixtures/approval-native-policy.json)来自标准csi模型。SignApproval_Validate要求表和条目均Routed，核对允许的决策、原审批人或允许角色替代，并调用CheckRoleAndOrg。SignApproval_Execute更新原条目与签批历史，并调用Complete，可能推进后续流程，不能伪造为普通字段更新。

RouteApproval_PreValidate可能ClearApprovals；Execute可能重排审批层级、路由和AutoMove。因此本工具不调用RouteApproval、RouteApprovalSheet、SignApproval、SignatureApproval或取消类服务，**包括execute=false**，也不把它们当安全读取方法。普通查询仅使用固定只读SQL、建模只读GET和QualityObjectInquiry。

标准MDB源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。它不证明当前部署自定义一致；未找到的探索枚举/方法保留原失败记录，不作为已实现决策码依据。

## 验证与后续

证据见[汇总](quality-fixtures/approval-summary.json)、[针对性JUnit](quality-fixtures/approval-focused-junit.xml)、[完整JUnit](quality-fixtures/approval-junit.xml)及[离线HTTPX守卫](quality-fixtures/approval-network.json)。模拟响应、临时计划库和确定性问法测试不表示现场员工获准审批或真实多轮业务成功率；守卫不是操作系统级网络隔离。

本轮没有真实审批、路由、签名、通知或服务重启。后续仍需实现固定审批表/条目/周期的计划绑定、决策与评论要求、通知及流程副作用核对、确认单次提交、未知结果证据和签名凭据的受控交互；不可在模型对话、日志或计划中持久保存口令。审批执行和电子签名尚未完成。

新增37项测试，针对性256项通过；全量3166项测试、2项子测试通过，其中质量相关1960项，父pytest真实HTTPX网络尝试0。[工具与参数核对](quality-fixtures/approval-manifest.json)确认66个质量工具均注册可分发，输入模型封闭，4组单轮/追问夹具保持审批上下文只读。

[现场元数据只读检查](quality-fixtures/approval-live-readcheck.json)未返回可用JSON，不能判断表为空、审批可执行或账号获准签批；本轮未选择真实审批记录预览或执行。
