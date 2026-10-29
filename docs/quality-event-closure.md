# 基础Event关闭前置诊断

2026-10-29新增 `get_quality_event_closure_context`，当前代码共60个质量工具。本轮交付只读诊断，不开放事件关闭计划或执行。既有CAPA关闭和NCR质量侧收尾入口不替代Event关闭。

## 参数与使用

必填 `event_name`、`organization_name`，可选 `event_instance_id`绑定原持久ID。问法如“查看组织Org下事件E关闭前还有哪些阻塞”，先调用本工具，按返回的阻塞项和未核实项继续查询。不为该问法调用CAPA关闭工具或自动关闭关联NCR。

结果包含原事件、组织签名策略、静态阻塞、最多20条关联NCR、原生查询审批上下文及Close处置候选。`complete=true`仅表示本次受限读取完整；`ready_for_execution`始终false。没有阻塞也不代表当前账号获得关闭许可。

## 校验范围

固定元数据字段核实后，按名称和可选ID读取基础Event7489。核对组织持久身份、冻结/变更计数和质量签名策略；读取Employee凭据等无关字段不在此工具范围。

报告非Active、冻结、未提交、未首次分流、已有关闭信息、CAR要求、流程或模板、检查表、直接审批表及当前查询审批要求。关闭签名事务为7712，其他事务的签名要求不被误认为关闭签名；全局质量签名或组织冻结等不满足共享配置读取前置时，返回complete=false及明确错误。

存在CAR要求仅表示要核实关联，不证明缺失CAR；存在流程仅表示要核实完成状态，不证明未完成。关联NCR不超过20条，按原事件ID核对NCR/容器ID及变更计数，超过范围、重复或身份不符则拒绝部分结果。关联NCR列表不代表全部批次或CAR关系。

独立Inquiry必须完整提供状态、类别、组织、归属、流程和审批字段，并与持久状态/类别/组织一致。Close处置原码来自当前原生候选，不猜测默认值。最后重读原事件和组织签名策略；变化则不返回完整诊断。多服务读取不提供数据库事务级一致性。

固定仍未核实的项目包括有效CAR/CAPA关联、流程和检查表最终完成、全部关联批次处置、当前账号关闭动作权限、动态审批与签名以及部署关闭副作用。工具不接受IgnoreProcessModel、签名口令、审批状态或关闭原因输入，不持久化计划、不提交业务。

## 原生依据与边界

[标准模型字段和服务目录](quality-fixtures/event-close-native-index.json)确认CloseQualityObject类型7712，原生Validate依次检查状态、权限、责任人和关闭条件。[执行逻辑](quality-fixtures/event-close-native-execution.json)中的ValidateClose会在要求CAR时查询CAR/CAPA交叉引用、核对流程阶段并检查未处置批次；ValidateApprovalStatus检查完成转换审批；CloseMfgNCR可能执行NCRCloseQuality。归档中失败的探索性CLF名称保留原错误，不作为实现依据。

标准csi MDB源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。标准元数据不是部署自定义一致性的证明，本轮没有真实目标提交、通知或服务重启。模拟查询不表示现场权限、联动或最终事务已验收。

测试记录见[本轮汇总](quality-fixtures/event-close-summary.json)、[全量JUnit](quality-fixtures/event-close-junit.xml)和[离线HTTPX守卫](quality-fixtures/event-close-network.json)。全量测试使用模拟接口与临时计划库，守卫不是操作系统级网络隔离。下一步仍需基于这些条件实现受限关闭计划、预览、单次提交及最终历史核实，不能把本入口当作该步骤已完成。

本轮新增21项测试，针对性59项通过；全量2886项测试、2项子测试通过，质量相关1680项。父pytest真实HTTPX网络尝试为0。60个质量工具均已注册并可分发，新增参数模型拒绝额外字段。
