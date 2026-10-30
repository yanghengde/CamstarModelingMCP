# Pending事件草稿资料维护闭环

日期：2026-10-30。代码及离线验证状态见[统一交付清单](quality-completion-plan.md)。本说明不代表已部署或已通过真实MES验收。

## 入口与出口

入口为已知原ID的Category1、Pending2基础Event，未提交、未分流、未冻结，具有明确原责任人和角色。原简述、报告人、发现人、优先级和日期可以不完整。出口为原ID的一次原生UpdateEvent保存，固定Submit=false；资料仍可不完整，保持Pending。保存与正式提交是两个独立计划。

支持明确修改简述、说明、发现区域、报告人、发现人及二者的组织、优先级、发生时间和报告时间。当前范围不修改事件组织、责任人、角色、分类/子分类、状态、流程、检查表、审批、交叉引用、自定义EventData、法规报告或关联NCR。缺分类的原草稿可以保留，但本入口不能补分类，仍不能据此正式提交。

## 已核实的原生行为

标准模型UpdateEvent继承的CreateEvent_PreValidate在Submit=false时调用DisableFieldValidations，并设置Transaction::IsExecute=true。因此普通`POST /api/UpdateEvent?execute=false`不是可信的保存预览，可能发生真实执行。此行为同样影响已提交Event的文字维护和拒绝/取消审批后的文字修订；本轮一并移除这些入口的普通假预览，保留原有输入与权限范围。

准备阶段仅执行固定`PerformEvent Load`，读取原明细、原生Edit动作及上下文，生成本地静态差异并独立重读。没有试写或修改后请求回显，返回`static_and_echo_verified=false`、`static_preview_only=true`、`patched_request_echo_verified=false`。固定Load沿用已核实的独立事件契约，不能由调用者指定任意事件。

标准UpdateEvent_FinishModifyEntity仅在Submit=true时调用SubmitEvent；后者设置IsSubmitted/Active并调用AutoTriage。草稿保存不进入此分支。代码不通过手工改标志模拟提交，也不把标准模型证据等同于实际部署没有扩展副作用。

标准QualityTxn签名处理在本事务Submit=false时不派生质量口令要求；仍读取组织策略并拒绝Load中出现签名要求/明细的未知分支。本入口不采集签名，也不满足后续Submit=true的签名要求。通用多签/协签执行门禁继续关闭。

原生只读证据：[草稿CLF](quality-fixtures/event-draft-native-flows.json)、[UpdateEvent绑定](quality-fixtures/event-submit-native-bindings.json)、[提交处理及签名分支](quality-fixtures/event-submit-native-processing.json)。来源为只读标准Base_InSite.mdb，SHA256为`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`；未修改模型。

## 工具和参数

1. `get_quality_event_update_context(event_name, organization_name, purpose="edit_pending_draft", event_instance_id=可选原ID)`：读取原资料、保持值和已知提交缺项，不生成计划，执行及提交就绪恒false。默认purpose仍为已提交事件的`update_text`；正式提交诊断仍用`submit_pending`。
2. `prepare_quality_event_draft_update(event_name, organization_name, changes, comments, event_instance_id=可选原ID)`：产生不可变计划和变更后请求。comments须非空，最多200个UTF-16字符；有歧义时绑定原ID。
3. 审阅后由系统确认`execute_quality_transaction(plan_id, plan_digest)`：固定`UpdateEvent execute=true`、`Submit=false`，只尝试一次。
4. `inspect_quality_transaction`只读原ID及有界原生历史；unknown或in_flight须核实最终原生事务，由人工`review_quality_transaction`审计后才可解除占用。
5. 确认保存结果后，独立调用`prepare_quality_event_submission`重读原资料和全部正式提交门禁；再经系统确认执行，不能把两个动作合成自动提交。

changes为拒绝额外字段的封闭结构：

| 字段 | 含义与限制 |
|---|---|
| brief_description | 简述，最多100个UTF-16字符；草稿可明确空串清除，但正式提交要求非空 |
| description | 说明，最多4000个UTF-16字符 |
| discovery_area | 发现区域，最多30个UTF-16字符 |
| reporter_name / initiator_name | 明确报告人/发现人名称，唯一有效Employees引用 |
| reporter_organization_name / initiator_organization_name | 明确报告人/发现人组织名称，唯一有效Organizations引用 |
| priority_level_name | 明确优先级名称，唯一有效PriorityLevels引用 |
| occurrence_date / reported_date | 带明确时区的日期时间，最多7位小数；成对产生本地/GMT值，保留时刻精度 |

省略或null均保留原值，不表示清除引用或日期；文本显式空串才清除。禁止NUL、超长或静默截断；拒绝无变更计划。已有时间成对不完整、Load偷偷补默认值、原始保持值不一致、未知扩展资料或最终报告时间早于发生时间，均停止准备，不能猜当前人员或时间补足。

原始空引用/空日期仅在原生Load忠实返回空时允许保留；若实际部署会自动补值，此分支需要单独核实，并不因为业务想补资料就允许回写猜出的默认值。

## 身份、执行与结果边界

草稿入口要求Load返回实际当前Employee，且等于原责任人，并具有Edit2、UserIsOwner、UserHasRole及原生`dbo.csiAuthGetAllowedUsers(Organization.Name, Role.Name)`授权。读取授权函数定义摘要、完整有界员工角色和参考配置版本，准备时重读，执行前重建摘要。报告人/发现人只是资料引用，不能成为执行身份。

查询、原资料读取和执行使用验证证书的HTTPS通道；Authorization/Cookie仅在当次内存中绑定，不进入工具参数、计划或日志。Load建立/刷新Cookie时停止，不借用新会话继续。受保护执行不保存可能反射会话内容的响应正文。

原生草稿分支会禁用字段校验，本地允许字段及保持值核实不能证明全部动态规则已通过，也不能证明实际部署最终保存成功。HTTP200、当前值相同和缺项清零均不表示执行或提交许可。

HTTP400、超时、取消及尝试后日志故障均不得自动重试；原事件及关联资源占用持续到最终人工审计。专用历史按原Event ID、原标记、UpdateEvent类型7578、父历史/事务关联和`Submit=false`读取，最多20条，超限/重复/不符不返回部分证据。`Submit=true`正式提交历史不能充当草稿保存历史。历史候选只作证据，不自动归因、解锁或推进分流。

## 连续问法与验证范围

- “查询事件E草稿保存条件，不要执行”：读取编辑上下文，执行器不暴露。
- “把事件E草稿的报告人改为明确员工，发生时间改为明确带时区值，先保存”：准备封闭changes，展示原值及变更，确认后只保存。
- “刚才超时了，再保存一次”：查原计划及最终事务证据，保持占用，不能新计划绕过。
- “确认保存后正式提交”：先完成原保存最终审计，再独立准备正式提交；签名、AutoTriage/通知及完整资料要求重新核验。

离线连续脚本覆盖：不完整草稿→显式补齐→保存后仍Pending→unknown阻止实际提交→隔离人工最终证据审计→独立Submit=true→独立最终审计→首次分流；以及空值保持、明确清除、缺项继续保存、身份/授权/配置漂移、复杂关联、未知扩展、错误历史、HTTP400/超时/取消/日志故障。所有业务状态和人工证据均为隔离模拟。

尚未真实创建、编辑、提交、分流或发通知，未改账号/组织配置，未部署或重启运行中的服务。实际部署契约、Load默认行为、TLS/会话、原生权限、保存效果和持久历史仍须C0现场验收。

下一独立范围：草稿分类/子分类维护及组织唯一组合核实。复杂BusinessRules、自定义EventData、检查表、关联NCR草稿及正式提交签名继续分开推进，不把本轮称为整个质量模块完成。
