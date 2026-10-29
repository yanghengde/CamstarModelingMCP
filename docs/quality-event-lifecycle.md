# 事件实际创建、开启、重新分派及复杂关联关闭

2026-10-29。新增 `prepare_quality_event_creation`、`prepare_quality_event_open`、`prepare_quality_event_reassignment`，并扩展 `prepare_quality_event_closure(allow_complex_context=true)`。当前65个质量工具。本轮提供可执行代码与模拟回归；没有真实MES事件提交或业务验收。

## 共同执行流程

`prepare_* → 展示原始资料、原ID/自动编号方式、目标字段、通知策略及未验证项 → execute_quality_transaction系统确认 → 单次原生请求 → unknown保留占用 → inspect只读证据 → 最终证据人工review`。

准备与执行前重建都核对持久身份、源记录和相关配置。非执行回显、本地静态计划、当前状态一致或HTTP200均不是业务批准、电子签名或最终成功。取消/超时/HTTP错误后不自动重试，不清库解锁，不用换工具或改名称绕过占用。工具输入模型封闭，不允许任意服务名、任意明细或流程/签名绕过字段。

## 实际创建

`prepare_quality_event_creation`必填组织、分类/子分类、优先级、简述、报告人、发现人及带时区的发生/报告时间；可选说明、发现区域、备注及allow_notifications（默认false）。时间保留7位小数；报告时间不得早于发生时间。所有人员均明确，不补当前用户；签名要求非空则拒绝。

**仅支持服务端自动编号，不接受指定事件名称。** 原生CreateEvent可复用同名未提交事件，提交前查名称无法消除与外部创建的并发窗口；自动编号路径不把用户指定名称传入服务，不用CreateEvent更新已有草稿。

组织必须有唯一Event类别1的有效NumberingRuleMap；当前首版要求编号规则未冻结、不回卷、DisallowSkipping类型1且有效序列长度不超过30。只读绑定映射和规则配置及变更计数，不预取或手工推进编号序列。分类组合必须唯一、未冻结，且直接Owner/Role明确，无检查表模板或失败组；不依赖未核实的CategoryMap归属回退。

Submit固定false，原生创建Pending且IsSubmitted=false，不自动首次分流。原生GetOwnerRole可应用分类默认归属，并按组织SMTP/通知策略发送待分派通知；存在通知配置时须明确allow_notifications=true，预览中展示策略及归属候选。本工具不额外发送邮件。

创建准备只做只读资料、配置、编号和当前Swagger核对，生成本地静态计划，**不调用CreateEvent execute=false或PerformEvent**。原生PreValidate修改Transaction::IsExecute，部署交互未核实，因此没有伪造“原生预览通过”；static_and_echo_verified=false、native_preview_requested=false、dynamic_validation_verified=false。实际执行仅一条CreateEvent execute=true，由原生最终检查和分配名称。

执行返回只选择安全的事件引用、完成消息和事务评论，不请求口令或人员凭据。新持久ID在提交前未知：unknown保留整个组织事件占用，与同组织的创建、分流、更新、开启、分派和关闭互斥。inspect仅把执行响应中的QualityObject ID作为候选；无有效ID不按名称猜测。响应ID与当前记录本身仍不能证明本次创建，需最终历史人工审计。原生编号唯一性、并发及部署自定义仍待真实验收。

## 事件开启

`prepare_quality_event_open(event_name, organization_name, event_instance_id?, comments?)`仅接受Closed=5、有关闭时间、已提交已分流且未冻结的基础Event7489。当前排除事件数据、批次/NCR联动、CAR要求、流程/检查表、审批和签名。

准备原生OpenQualityObject（8412）execute=false回显，核对原ID、归属、角色、状态及无绕过/签名上下文。Open没有在已确认的动作枚举中独立动作位，工具不编造“Open权限位”；预览中的动作/账号上下文不是最终执行许可，MES仍做原生校验。

原生ApplyDetails只设置Active；**不手工清除旧CloseDate或关闭人等历史信息**。存在EventData的开启可能增加容器质量控制计数，当前入口排除该路径，避免重复增加。开启后若保留旧关闭时间，后续需显式复杂关闭入口核对当前Active状态，不通过手工清历史字段解决。

## 重新分派

`prepare_quality_event_reassignment`明确原事件/组织、新owner_name和role_name，可选原ID、备注、allow_notifications（默认false）。仅Active已提交已分流、未冻结的独立基础Event，无事件数据、关联NCR、CAR要求、流程/检查表、审批或签名。

持久核对原/新人员及角色，读取人员仅选择安全身份字段；原账号须为原责任人且有原角色，原生可用动作含ChangeOwner位4。新责任人与角色完全不变时拒绝空操作。保持TriageComplete=true，不借分派触发首次分流。

原生ChangeOwner（7728）ValidateAssigneeRoleCombination负责最终核实目标人员、角色和组织组合；静态解析到有效引用不代表组合获准。服务端拒绝不会被当作成功。配置可能发送所有权分派通知时，须明确允许后审阅计划；不清空组织通知配置，不额外发送通知。

## 复杂关联关闭

既有 `prepare_quality_event_closure`默认保持原受限模式；显式 `allow_complex_context=true`扩展支持：

- 原Active、已提交/分流的基础Event，保留CAR策略。
- 有效CAR/CAPA交叉引用，最多50条；TrackingId必须指向原事件，目标须存在、同组织且未Deleted。要求CAR时必须核实至少一条Category2或5关联，不用其他类别冒充。
- 原ProcessModel身份、Parent、Stage、冻结及版本快照；不把某个猜测的Stage值视为完成，不设置IgnoreProcessModel。原生最终比较CompletedStage/Voided及批次处置要求，可能拒绝尚未完成的流程。
- 最多20条原关联NCR，绑定事件/容器ID及版本并占用相关容器；这里不执行NCRCloseQuality子请求，不把事件关闭说成NCR已关闭。
- 最多20张直接审批表，必须原生Approved=40；完成审批引用须对应这些原表。拒绝当前待审批入口、待批准/拒绝/取消状态、额外签名、仍需启动审批或路由模板的关闭，不代签、不撤回、不重启审批。

仍拒绝EventData、检查表执行和会触发制造NCR/批次自动处置的子事务。对这些部署组合，没有完整资料、原生参数与事务证据前不开放。复杂计划不是“所有关联均已处理”的证明；若MES动态验证拒绝，保留unknown证据和占用，不绕过验证强行关闭。

源关联、流程、审批及质量解决代码在准备后及执行前重读，变化则停止提交。CAR/CAPA和容器依赖也纳入占用；CAPA更新/关闭增加同一质量对象ID占用，减少跨入口绕过依赖的风险。这仍不代表与所有外部程序或其他日志协调。

## 原生依据及验证边界

[生命周期目录](quality-fixtures/event-lifecycle-native-index.json)、[开启/分派逻辑](quality-fixtures/event-lifecycle-native-execution.json)、[字段与默认值](quality-fixtures/event-lifecycle-native-permissions.json)、[关系字段与表列](quality-fixtures/event-lifecycle-native-relations.json)、[审批状态与列](quality-fixtures/event-lifecycle-native-complex.json)、[关联语义](quality-fixtures/event-lifecycle-native-tracking.json)及[编号配置](quality-fixtures/event-lifecycle-native-numbering.json)来自只读标准csi模型。QualityCrossReference.TrackingId/CrossReferenceId才是质量关联；EventCurrentCrossRefs属于历史关联，不混用。

源MDB SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不是部署自定义一致性证明。失败的探索名称和编译方法读取保留原错误，不冒充已解析逻辑。ChangeOwner的离线Swagger由原生字段构造明确测试响应，不作为当前现场契约证据；运行时须实际GET当前各服务Swagger，否则不能准备计划。

测试及交付证据见[汇总](quality-fixtures/event-lifecycle-summary.json)、[针对性JUnit](quality-fixtures/event-lifecycle-focused-junit.xml)、[完整JUnit](quality-fixtures/event-lifecycle-junit.xml)、[离线HTTPX守卫](quality-fixtures/event-lifecycle-network.json)。模拟接口、临时计划库及工具确认检查不等同真实业务验收；守卫不是操作系统级网络隔离。

本轮没有真实创建、开启、分派、关闭、通知或服务重启。实际部署权限、原生编号、邮件、流程完成、审批、创建ID与最终历史归因仍需现场验收。后续范围包括手工指定名称的原子创建、事件数据/批次/制造NCR子事务、待审批及电子签名，不将本轮说成全部质量功能完成。

本轮新增89项测试，针对性524项通过；最终全量3083项测试、2项子测试通过，其中质量相关1877项，父pytest真实HTTPX网络尝试0。[工具/参数与问法核对](quality-fixtures/event-lifecycle-manifest.json)确认65个质量工具均注册且可分发，新增参数模型封闭，确认入口保持原6个。实际质量计划库只读检查仍为2条cancelled，没有新增现场执行记录。

[现场只读核对](quality-fixtures/event-lifecycle-live-readcheck.json)未取得有效元数据或当前三个服务Swagger；[不保存响应正文/凭据的传输诊断](quality-fixtures/event-lifecycle-live-transport.json)记录ConnectError。不能据此判断表为空、账号有权限或服务支持这些事务，恢复连接后仍需逐项验收。
