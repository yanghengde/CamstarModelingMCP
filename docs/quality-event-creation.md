# 质量事件创建资料与组织策略核验

2026-10-29新增 `validate_quality_event_creation`，该阶段质量分组62个工具；后续新增[生命周期执行计划](quality-event-lifecycle.md)，当前66个工具。本轮交付创建前的只读资料核验，不开放创建请求、草稿保存或提交。不能把核验报告说成事件已经创建。

## 参数及返回

| 参数 | 要求 |
|---|---|
| event_name | 明确名称，最多30个UTF-16代码单元；不自动编号 |
| organization_name | 明确组织 |
| classification_name、subclassification_name | 明确组织允许的分类/子分类组合，不选择首行 |
| priority_level_name | 明确优先级，不补默认等级 |
| brief_description | 非空简述，最多100个UTF-16代码单元 |
| reporter_name | 明确报告人，不补当前用户 |
| occurrence_date、reported_date | 均必填，带明确时区，最多7位小数；报告时间不能早于发生时间 |
| description、discovery_area | 可选文字，默认空字符串；分别最多4000、30个UTF-16代码单元 |

例如“核验事件创建资料”先取得上述明确业务信息，再调用本工具。省略必填信息不能以测试名称、当前账号或当前时间代填。工具参数模型拒绝submit、owner、任意serviceDetail、签名口令及流程绕过字段。

`complete`表示本轮有限读取和重读是否完整；`static_valid`仅表示已支持的静态输入/配置没有发现错误，未完成读取时为null。`ready_for_execution`恒false，`created_event_verified`恒false，没有plan_id。`validation_digest`只是核验报告摘要，不能传入执行器作为执行计划。

结果提供名称占用、固定参考配置身份/冻结/变更计数、报告人安全身份字段、唯一分类映射及默认Owner/Role候选、检查表/失败组引用、组织签名要求和SMTP/通知策略。默认Owner/Role只是配置中的候选，尚未应用，也未核实CategoryMap回退，不证明最终归属。

## 核验及隐私边界

已有名称一律报占用，包括尚未提交的草稿，不调用原生CreateEvent覆盖原草稿。名称查询固定SQL、严格限2条，不接受任意SQL；重复或不完整响应不视为可创建。

组织分类映射最多100项，组合必须唯一；引用配置或匹配映射冻结时报告静态错误。分类映射不会作为可传给CreateEvent的选择ID。报告人通过仅选择InstanceID、Name、IsFrozen的人员读取及固定变更计数查询，不读取凭据。

组织通知读取只选择SMTPTransport引用及NotificationEvents的身份、通知类型原码和AssigneeOnly；最多100项，分页、缺字段、重复或无效引用均不返回完整核验。SMTPTransport不展开，不读取账号、口令、邮件正文或实际收件人，只输出是否配置传输以及受限通知条目；不将未配置SMTP等同为执行许可。

组织、分类、子分类、优先级、报告人及通知策略在返回前重读，名称占用也重查；变化则报告不完整。多服务读取不提供数据库事务级一致性，摘要不是现场锁定。发生/报告时间保留输入时区及7位小数，不猜现场时区，不换成当前时间。

## 为什么本轮不调用创建接口

原生[创建字段/默认值](quality-fixtures/event-create-native-index.json)和[创建执行逻辑](quality-fixtures/event-create-native-execution.json)确认CreateEvent类型7491、Submit默认false。StartModifyingEntity为新事件设置Pending及IsSubmitted=false；Submit=true才进入Active和AutoTriage。但FinishModifyEntity在Submit=false时仍调用GetOwnerRole及SendEventEMail，通知类型为待分派。

[归属和通知逻辑](quality-fixtures/event-create-native-notifications.json)显示GetOwnerRole从组织分类映射解析Owner/Role，不足时回退CategoryMap。SendEventEMail是否实际发送取决于SMTP、通知映射与收件人；因此不能说保存草稿必然无外部效果，也不能说一定发送了邮件。

CreateEvent_PreValidate在not Submit时调用DisableFieldValidations并设置Transaction::IsExecute=true。这说明部署上该内部标志与HTTP execute=false的实际交互需要进一步核实；**不是已证明execute=false一定写入业务数据**。在尚未核实前，本工具不请求CreateEvent任何方法，包括execute=false、PerformEvent或自动编号，因此不会因本次核验触发原生创建、归属分派或通知。

标准csi MDB源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`；标准模型不是当前部署自定义一致性证明。归档读取错误保留原状态，不作为成功发现的接口证据。本轮不修改MES、发送通知或重启服务。

## 证据与后续

测试见[本轮汇总](quality-fixtures/event-create-summary.json)、[针对性JUnit](quality-fixtures/event-create-focused-junit.xml)、[完整JUnit](quality-fixtures/event-create-junit.xml)及[离线HTTPX守卫](quality-fixtures/event-create-network.json)。模拟接口与临时计划库验证输入、同名草稿保护、唯一映射、冻结、时间精度、通知清单和数据变化，不证明现场权限或创建验收。HTTPX守卫不是操作系统级网络隔离。

后续仍需核实当前部署创建预校验/非执行语义、通知策略和默认归属，并设计可审阅的创建计划及一次提交、自动编号/重复名称并发保护、创建后持久ID与历史关联、异常结果占用及人工审计。本轮未将CreateEvent加入统一执行白名单，不使用CAPA或NCR升级代替创建事件。其他剩余功能见[高级功能计划](quality-advanced-roadmap.md)。

本轮新增46项测试，针对性365项通过；全量2994项测试、2项子测试通过，其中质量相关1788项，父pytest真实HTTPX网络尝试为0。[工具与参数核对](quality-fixtures/event-create-manifest.json)确认62个质量工具均注册且可分发；7组单轮/追问夹具检查可见性，不表示模型真实业务成功率。实际质量计划库只读检查仍为2条cancelled，本轮未新增现场计划。

[现场元数据只读检查](quality-fixtures/event-create-live-readcheck.json)未返回可用JSON，不能据此判断表为空或权限有效；没有调用原生创建、创建预览或自动编号接口。

后续进展：已另行实现自动编号实际创建计划，避免指定名称与现有草稿并发复用；准备仍不调用原生创建预览。详见[生命周期执行计划](quality-event-lifecycle.md)。本核验工具仍只读；新增automatic_numbering布尔选项，默认false，选true时event_name必须为空字符串，名称检查交由后续原生自动编号路径。
