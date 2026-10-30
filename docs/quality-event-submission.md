# C3第一闭环：Pending事件正式提交及首次分流衔接

2026-10-30。本轮补齐已有Pending草稿通过原生UpdateEvent正式提交、原资料保持、原生自动处理边界、未知事务审计及首次分流衔接的代码。真实部署契约、权限、通知和最终业务效果仍未验收，不能据此称整个事件生命周期已完成。

## 原生语义

UpdateEvent7578的FinishModifyingEntity在Submit=true时调用与CreateEvent共用的SubmitEvent逻辑：已IsSubmitted则拒绝；否则设置IsSubmitted=true、Status=Active1，然后调用QualityObject.AutoTriage。**正式提交会调用自动分流逻辑，不只是修改两个状态字段。**

AutoTriage取得原类别的TriageSpec，执行其BusinessRules，再按当前类别明细设置CAR要求；随后取得/验证Owner和Role、调用AssignChecklist并发送组织归属通知。标准AssignChecklist为Noop，不能据此假定实际部署也没有扩展。本轮要求完整原配置、空BusinessRules、唯一保留类别且不改归属/CAR、不挂流程/检查表/自动路由；遇到不支持的规则停止，不将规则置空或跳过。

在所读标准逻辑中，空BusinessRules路径没有直接设置TriageComplete。提交后的真实IsSubmitted、Status和TriageComplete均须读取；不能把调用AutoTriage解释成已完成首次分流，更不能自动追加TriageQualityObject。若实际已分流，现有首次分流工具会拒绝重复操作。

UpdateEvent的PreValidate与CreateEvent共用：Submit=false分支会DisableFieldValidations并把Transaction::IsExecute设为true。因此准备正式提交时不能给普通POST传Submit=false来模拟本操作。原资料读取使用已有固定PerformEvent Load，输入Submit=false只用于该只读事件；正式提交的非执行POST必须明确Submit=true、execute=false。部署自定义及REST调度的实际行为仍须现场核实。

## 范围及参数

新增 `prepare_quality_event_submission`，当前74个质量工具；已有 `get_quality_event_update_context` 增加 `purpose='submit_pending'`，默认用途仍是已提交事件文字维护，不新增重复的只读工具。

正式提交准备必填event_name、organization_name、非空comments；可加原event_instance_id消歧，allow_notifications默认false。没有Submit、Status、IsSubmitted、TriageComplete、Employee或原资料修改参数，也不接受任意原生事件名。只支持：

- CDOType7489、Category1、Pending2，未提交、未分流、未冻结，原Owner/Role/优先级、报告人、发现人和非空简述均明确。
- 原流程/原流程模板、检查表、事件数据、关闭信息、法规报告、审批表及关联NCR均为空。分类/子分类ID和原版本须核实，不修改分类。发生/报告时间必须完整且本地时间/GMT一致，报告时间不得早于发生时间，不补当前时间。
- 组织唯一类别映射、有效TriageSpec、唯一保留当前类别的明细；不改变Owner/Role/CAR，不自动路由或挂流程/检查表。BusinessRules必须完整展开且为空，空或缺失规则配置不按默认猜测。
- 原生Edit动作2、UserIsOwner及UserHasRole满足，IgnoreProcessModel=false，签名及审批要求明确为空；不能通过普通确认代签。本轮提交签名未开放，C2.B/C2.C已有其它签名能力不自动适用于此事务。

需要先修正草稿资料、补充不完整人员/日期、处理自定义数据或复杂自动规则时，返回具体阻塞；不在本入口顺便修改或制造替代草稿。

## 多轮问法与执行顺序

1. “查看组织Y事件X的草稿是否可以正式提交。” → `get_quality_event_update_context(event_name='X', organization_name='Y', purpose='submit_pending')`。读取原ID、资料、Load保持值、分流规则和通知策略，不生成计划、不提交；ready_for_execution=false。即使配置了通知，也可以只读查看，不把查询当作允许发通知。
2. “正式提交此草稿，说明……，允许配置中的通知。” → `prepare_quality_event_submission(…, comments='…', allow_notifications=true)`。仅可能通知时要求明确允许；通知配置未知则停止，不清空配置。
3. 准备固定Load核对原EventDetail7541，完整保持文字、Reporter/Initiator及组织、发生/报告本地时间与GMT、优先级、空事件数据/法规报告。剔除临时服务/父级/历史ID，不把整个原生服务回填。实际POST只包含原对象/组织、Submit=true、说明及保持后的serviceDetail。
4. Submit=true非执行回显核对完整载荷、原Pending状态/归属、当前权限和签名/审批门禁；最后独立重读原记录、分类、所有分流配置、BusinessRules、通知及Load原资料。任何变化阻止原计划。
5. 通过 `execute_quality_transaction(plan_id, plan_digest)` 系统确认后，仅一次UpdateEvent execute=true。不会调用CreateEvent覆盖同名草稿、直接改状态，或附加分流/移站/审批请求。使用原对象及组织资源占用，与创建、文字维护、转派、分流和关闭等操作协调。
6. 返回先unknown。`inspect_quality_transaction`只读取原ID的现状、有限质量历史和正式提交候选。UpdateEventHistory.Submit=true区别于普通文字更新；候选按原HistoryId、原请求评论，精确关联HistoryMainlineId/TxnId/HistoryId，并核对TxnType7578。最多20条，超限、缺字段、重复或串对象/请求均拒绝。
7. 当前Active/IsSubmitted、相同载荷、HTTP200或Submit=true历史都不自动认定本次请求成功，不能解除占用。须按既有 `review_quality_transaction` 取得最终原生事务证据进行人工审计；不自动填写审计结论，不允许未知事务用新计划重试。
8. 最终审计后，按真实状态读取 `get_quality_event_triage_context`。只有已提交Active且TriageComplete=false、其它配置/权限满足时，再明确准备/确认首次分流；若真实已分流则不补发。分流仍是独立业务决定，不随提交自动连发。

此前自动编号创建入口返回的原生ID只是候选。必须先核实创建最终事务及目标ID，完成创建unknown的人工审计，才能继续本正式提交链路；本功能不按名称猜测新建事件归因。

## 异常和未覆盖项

重复提交、状态/资料/权限/规则/通知变化、错误人员或分类、扩展明细、签名要求均停止。超时、400、中断仍unknown；提交后本地日志保存失败时返回无法确认而非“未提交”，保留in_flight并禁止重试。原对象与关联配置占用保留；换文字维护/分流工具、新建计划或改名称不能绕过。不把当前分流候选当作后续行动授权。

尚未覆盖草稿文字编辑/补资料、复杂BusinessRules自动分流、自动改归属或CAR、流程/检查表、自定义EventData、法规报告、关联NCR草稿、提交签名及指定名称原子创建。这些继续列为C3/相关闭环待办。本轮仅完成提交衔接，不扩展到整套事件建模或全部质量功能。本地占用不能锁住外部MES客户端或其它配置维护程序，最终权限/事务锁/执行校验仍由MES承担；部署并发及原生通知未经过现场验收。

## 原生依据与验证

标准MDB SHA256 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。证据：[服务/动作](quality-fixtures/event-submit-native-index.json)、[提交及PreValidate](quality-fixtures/event-submit-native-processing.json)、[方法绑定](quality-fixtures/event-submit-native-bindings.json)、[AutoTriage](quality-fixtures/event-submit-auto-triage.json)、[归属/检查表](quality-fixtures/event-submit-assignment-flows.json)、[历史列](quality-fixtures/event-submit-history-columns.json)、[历史映射](quality-fixtures/event-submit-history-maps.json)。标准模型和保存的Swagger测试夹具均不是实际部署一致性证明，准备阶段仍读取当前契约。

隔离脚本覆盖Pending→原生模拟提交→unknown阻止实际分流→虚构人工最终审计→首次分流；原生状态、历史及人工证据都是显式fixture和临时SQLite，不能当作Camstar引擎验收。另覆盖原资料不变、通知只读与明确允许、业务规则/权限/签名阻断、变化和超时/400/中断、防重、载荷覆盖、专用历史串联错误、多轮问法和MCP实际分发/确认。

结果：[本轮汇总](quality-fixtures/event-submit-summary.json)、[完整回归](quality-fixtures/event-submit-full-junit.xml)、[最终定向](quality-fixtures/event-submit-final-junit.xml)、[工具/问法核对](quality-fixtures/event-submit-manifest.json)。HTTPX离线守卫不是操作系统级隔离。本轮没有真实创建、正式提交、分流、通知、签批、部署或重启现用服务；现场契约、实际权限/通知及持久历史仍未验收。
