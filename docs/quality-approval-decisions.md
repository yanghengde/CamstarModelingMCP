# 原指定审批人的批准与拒绝计划

当前扩展见[角色替代审批](quality-role-approval.md)与[单人质量电子签名](quality-electronic-signature.md)：仅原Role2且明确允许、原生当前员工角色/组织核验通过才开放角色替代。组织要求本事务单人QualityESigDetail时，可明确require_quality_esign=true，使用独立本机页面输入并授权一次提交；不在聊天/工具参数输入口令。默认C1门禁保留，通用多签/协签及委托仍未开放，真实MES未验收。

2026-10-30当前范围见[完成审批及修订重提](quality-approval-cycle.md)与[统一交付清单](quality-completion-plan.md)：已增加模板挂接并路由、旧/新周期历史及Event/CAPA修订重提。以下带旧日期的阶段记录保留其当时边界和测试数量，不作为当前限制或验收结论。

2026-10-29新增 `prepare_quality_approval_decision`，当前68个质量工具。支持当前认证员工对其原指定审批条目提交SignApproval批准或拒绝请求，不提供角色代签、身份覆盖或额外口令电子签名。

## 参数与顺序

必填action（approve/reject）、name、organization_name、approval_sheet_id、approval_entry_id、approval_decision_id及非空comments（最多200个UTF-16代码单元）。object_kind为event（默认）或capa，可绑定instance_id；allow_notifications默认false。

先get_quality_approval_context读取明确对象、当前表/条目和该表允许的决策。原ID及决策必须来自真实资料；不能猜测第一条、按名称相似选择决策，或用表状态码作决策码。准备展示原周期、条目、实际决策码、原审批人、通知及未验证项，通过execute_quality_transaction系统确认后仅一条SignApproval请求。

## 身份、层级与决策约束

仅未冻结、已提交的Active/InReview直接Event/CAPA；无事件数据、检查表或嵌套流程。组织SignApproval事务7839不得要求当前入口未采集的额外签名。

原表必须Routed10、未冻结且有明确当前周期GMT值。原条目须属于该表、Routed10、未冻结、SubstituteOption=1，原审批人及角色明确，无已有决策、批准人、LastDecision或完成时间。原生当前账号EntryForApproval必须精确指向所选条目。前序层级未完成则拒绝，条目层级须有效，不将Routed或角色配置本身当授权。

决策必须属于该表实际绑定的未冻结ApprovalDecisionList，选择项未冻结，原生DecisionType与action一致：**Approved=10、Rejected=20、RoutingCanceled=30**。这些是决策码，与审批表Approved40/Rejected30/Cancelled20不同。只支持10/20，不把RoutingCanceled或自定义未知决策解释为批准、拒绝或退回。

comments必须是用户明确提供的非空说明，不以生成的关联标记满足评论要求。IncludeComments原值仍展示，但不因false而允许空白操作说明。

prepare调用当前SignApproval Swagger核实引用类型，再execute=false核对原对象、表、条目、决策及说明完整回显。表/决策使用NamedSubentityRef并绑定父ID，条目使用SubentityRef并绑定表ID；不混用引用类型。

原生Employee必须与原条目ApproverId一致，AvailableActions须含Approve512；不要求当前审批人是对象责任人，也不把责任人权限当作签批身份。组织、对象类别/状态、当前审批条目及无流程上下文须一致。ESigRequirement和AdditionalESigReqs必须明确为空；员工角色/组织成员资格由原生最终CheckRoleAndOrg验证，预览不认证该资格。

## 签名与通知

本功能使用当前MES认证会话提交原生审批决策。**不等同于完成额外口令电子签名**。不接受Employee、ApprovingFor、ApprovingForRole、Password、签名覆盖或ProcessESignatures参数；要求额外签名时拒绝，不以系统确认代签，不在对话、计划、日志中收集口令。

原生Complete/AutoRouteDetails可能路由后续层级并发送审批分派或批准/拒绝结果通知；配置可能通知时必须明确allow_notifications=true。工具不单独发邮件、不展开SMTP凭据，不清空通知配置。实际执行响应只选择安全的对象/表/条目/决策引用、完成消息及评论/ACE字段，不请求整个服务的Password或人员对象。

提交Rejected决策可能仍等待其他同层或后续审批；原生在后续条件满足时再整体Reject。**不保证整表立即Rejected，不是退回修改或撤回，不强制修改父对象状态**。当前首版没有退回操作。

## 防重和证据

准备后及执行前重读完整对象、审批表/周期/条目、决策、原审批人及角色和通知策略；变化则停止提交。原对象与原审批表共享占用，与取消、关闭、分派及CAPA维护互斥，避免同一对象通过其他工具绕过unknown。

执行后先unknown，HTTP错误/超时/取消均不自动重试。inspect固定原对象、原表和原条目ID，当前Approved/Rejected、相同决策或LastDecision仅作证据，不自动归因本次成功或解锁；最终事务历史由人工review。当前账户权限、签名、通知和所有外部程序的并发不由本地日志单独保证。

## 原生依据与边界

[决策及完成逻辑](quality-fixtures/approval-decision-native.json)、[签批字段与Approve512](quality-fixtures/approval-decision-native-fields.json)、[父对象通知及后续层级行为](quality-fixtures/approval-decision-native-effects.json)来自只读标准csi模型。SignApproval.Validate要求表/条目Routed、允许决策、原审批人及CheckRoleAndOrg；Execute写决策和SignHistory后调用Complete/AutoRouteDetails。

标准MDB源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不证明当前部署自定义一致性。离线Swagger明确由原生引用类型构造测试响应，不冒充当前现场契约。

证据见[汇总](quality-fixtures/approval-decisions-summary.json)、[针对性JUnit](quality-fixtures/approval-decisions-focused-junit.xml)、[完整JUnit](quality-fixtures/approval-decisions-junit.xml)、[离线HTTPX守卫](quality-fixtures/approval-decisions-network.json)。模拟接口和临时计划库不能证明现场签批成功，守卫不是操作系统级网络隔离。

本轮没有真实批准、拒绝、签名、通知或服务重启。实际权限、原生决策、通知、层级推进和最终历史需现场验收。角色替代/委托、退回、嵌套流程、事件数据及受控口令签名仍未开放。

本轮新增56项测试，针对性236项通过；全量3272项测试、2项子测试通过，其中质量相关2066项，父pytest真实HTTPX网络尝试0。[工具/参数与问法核对](quality-fixtures/approval-decisions-manifest.json)确认68个质量工具均注册且可分发，参数模型封闭，5组查询/操作问法选择明确。实际质量计划库只读检查仍为2条cancelled，没有新增现场执行记录。

[现场SignApproval Swagger只读检查](quality-fixtures/approval-decisions-live-readcheck.json)未取得有效定义，不能判断账号有权限或签批可执行；没有选择真实审批条目预览或提交。本轮不重启服务。
