# 已有事件扩展资料的受限维护

2026-10-30。当前交付范围是已有标准EventData的**问题描述、设备位置**维护，包含读取、静态准备、确认后单次原生保存、未知结果占用和人工最终审计后的再次维护。实际MES尚未验收，未部署或执行真实业务。

## 原生依据与边界

标准UpdateEventData（8764）通过固定Load调用GetDetails，读取原Event及EventData。ModifyDetails使用UpdateEventDataToEvent完整回写原Event字段，再以EventDataDetailsToEventData映射28项扩展值到原QualityObject.EventData。后者明确包含ProblemDescription及DeviceLocation；这为实际保存提供了依据，并非仅有可写ref或成功回显。

UpdateEventDetails_PreValidate会**无条件设置Transaction::IsExecute=true**，因此不能用普通`POST /api/UpdateEventData?execute=false`预览。准备只调用固定`PerformEvent Load`并生成本地差异；只有统一执行器经系统确认后才调用一次真实保存。返回static_and_echo_verified=false、static_preview_only=true，不声称修改后载荷回显或动态原生校验已通过。

父ShopFloor_ApplyDetails只在定义ServiceDetail/ServiceDetails时调用相应明细处理，或在ParametricData存在时执行采集明细。标准UpdateEventData没有这两个通用明细字段；本入口核对当前契约不引入这些字段，并要求Load的DataCollectionDef/ParametricData为空。原生数据修改CLF只运行已核实的标量映射及历史创建；不把EventDataDetails的客户、NCR评论、自定义内容或勾选字段处理流程当成这两个文字字段的通用写入接口。

标准EventCustomData（8163）及EventCustomDetail（8166）基类没有业务字段。实际现场自定义字段必须提供部署类型、字段、原生明细及持久保存映射等证据，当前未实现任意字段维护。已有EventCustomData、未知派生数据、客户或NCR评论等复杂上下文会阻断本入口；不会丢弃未知字段后保存。

原生证据：[数据/明细/历史模型](quality-fixtures/event-custom-native-models.json)、[Load/修改CLF](quality-fixtures/event-custom-native-flows.json)、[执行与预校验](quality-fixtures/event-custom-execution-flows.json)、[父级ApplyDetails](quality-fixtures/event-custom-super-apply.json)、[标准映射](quality-fixtures/event-classification-native-maps.json)、[数据库列](quality-fixtures/event-custom-database-columns.json)、[历史及禁用清单列](quality-fixtures/event-custom-history-list-columns.json)、[关联表](quality-fixtures/event-custom-linked-tables.json)。只读标准MDB SHA256：`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。未修改标准模型，标准证据不替代部署验收。

## 工具与参数

质量组新增两个工具，当前共77个：

| 工具 | 输入和结果 |
|---|---|
| get_quality_event_data_context | event_name、organization_name、可选event_instance_id。读取原数据、原Event保持值、契约及当前上下文；无计划、执行就绪恒false |
| prepare_quality_event_data_update | 同上，另需封闭changes及非空comments；生成不可变本地计划和请求预览 |

changes仅允许：

| 字段 | 业务含义 | 上限 |
|---|---|---|
| problem_description | EventData.ProblemDescription，事件扩展资料中的问题描述 | 255个UTF-16字符，依据实际标准持久列，不沿用Event.Description的4000上限 |
| device_location | EventData.DeviceLocation，记录中的设备位置文字 | 30个UTF-16字符；不移动设备或维护资源实体 |

至少一项明确变化。省略/null保持原值，文本显式空串清除；拒绝NUL、超长、无变化、身份/状态/分类/任意字段，不截断或自动补写。comments须非空，最多200个UTF-16字符；实际带唯一事务标记。

MCP入口与Agent直接JSON调用均先校验同一封闭结构，再生成计划。同步修复原prepare_quality_event_draft_update对Agent直接JSON调用的兼容性，保留其原范围与类型约束；未知字段错误不反射输入值。

## 入口条件与原值保护

- 原对象是已提交Active1、未冻结Category1基础Event，具有原责任人/角色/优先级及已有8085 EventData；不创建、替换数据，不从草稿创建扩展结构。
- 无流程/原流程模板、检查表或关闭信息，无直接审批表、法规报告、关联NCR；数据无客户、自定义数据、NCR评论或编号，无批次、失败、日志、动作及疑似设备明细。
- 读取原Event、EventData及分类/组织配置的持久ID和ChangeCount；原EventData归属与类型必须匹配，完整源字段不得缺失。
- 固定Load严格核对原Event文字、报告人/发现人/组织/优先级和本地/GMT日期，与SQL原值一致，不接纳派生默认人员或时间。所有28项扩展映射值完整保持，只有明确两项可以改变；职业/补偿引用保持原ID，既有空值保持空，原日期保留7位小数并要求本地/GMT为同一时刻。
- EventDisallowedTxns使用原Event ID、原字段25978和序号完整读取，最多100条，要求原生Load回显一致；保留原顺序，拒绝重复/未知/超限或明确禁止8764的配置，不清空禁用清单后保存。
- 当前实际Employee须为原责任人，原生Edit2、UserIsOwner、UserHasRole及原组织/角色授权函数csiAuthGetAllowedUsers均须核实。核对授权函数摘要和完整有界角色快照，不传Employee、用户或口令参数。

组织若要求8764事务或所有质量事务电子签名，当前入口拒绝，不以普通确认替代签名。Load派生签名明细/额外要求或采集要求也拒绝。通用多签/协签执行门禁和分类修改未实现的限制均保持。

实际原生ModifyDetails可能重新执行根ReportedDate/OccurrenceDate的本地/GMT换算。本轮只发送已核实的原保持值，不提供日期修改参数；真实部署时区、原生换算效果仍需现场验收，不能仅凭本地模拟承诺不会产生任何时间差异。

## 执行、异常与结果核实

准备和执行前均独立重读，重建原Event、数据、全部所选配置、分类映射、禁用清单及员工授权摘要。变化使旧计划失效。共享占用包含原Event、原EventData及配置引用；不通过改名、新计划、换工具或替代数据绕过unknown。

所有受保护读取和执行使用验证证书的HTTPS，Authorization及有效Cookie只在当次内存绑定；Load若建立/刷新会话即停止，不自动换会话重试。计划及输出不保存凭据。受保护执行不保存响应正文，读取异常不反射可能包含会话的远端正文。

审阅原值与拟改值后，通过execute_quality_transaction(plan_id, plan_digest)系统确认单次POST UpdateEventData，固定execute=true，不附加提交/分流/审批/关闭、改分类或客户/批次处置。HTTP200/400、超时和取消均不自动归因成功；尝试后日志故障返回未知，保留in_flight。unknown继续占用，最终原生证据人工review后才可继续。

inspect_quality_transaction始终读取原Event ID和原EventData ID，不跟随外部改写的数据关联。原生历史实际存储于共享UpdateEventDetailsHistory表，类型为UpdateEventDataHistory **8752**；关联HistoryMainline时还要求TxnType **8764**、原对象、原事务关联及唯一标记，最多20条，错误类型/重复/超限拒绝。不存在凭类名猜出的UpdateEventDataHistoryId表列。候选历史仅证据，不自动归因、解锁或重试。

本地锁只能协调本工具计划，不能消除外部客户端在最后重读后的竞态。服务端原子版本条件、最终事务关联及部署副作用未验收，现阶段仍需人工最终证据。

## 问法与连续验证

| 问法 | 处理 |
|---|---|
| 查询事件E扩展资料维护条件，不要保存 | 只读上下文，不生成计划或暴露执行器 |
| 把事件E的问题描述改为明确文字 | 准备封闭changes，审阅并确认后保存 |
| 查看事件E扩展资料；把设备位置改为站点B | 继承事件扩展资料主题；保持设备位置是文字记录的含义 |
| 改为查看/修改资源R的设备位置 | 识别明确资源话题切换，不套用事件数据工具 |
| 刚才超时了，再保存一次 | 查询原计划及最终证据，保持占用，不重复尝试 |
| 修改任意自定义字段、纠正/预防评论或客户资料 | 说明现有适配范围，要求后续部署字段及原生映射接入；不传通用字段字典 |

离线连续模拟覆盖读取→明确修改两项→原Event及其它扩展值保持→unknown阻止重复/第二计划→候选历史→隔离最终人工审计→下一次独立保存。负向覆盖错误原ID/类型/归属、未知结构、复杂关系、数据/配置/授权漂移、普通假预览禁止、字段长度/空值、7位时间、HTTP400/超时/取消/日志故障、真实HTTP读取层Cookie刷新拦截、异常信息脱敏、Agent直接JSON及MCP调用和多轮问法。状态及人工证据均为隔离模拟。

尚未真实修改、提交、关闭事件或发送通知，未改数据/组织配置/账号、部署或重启服务。代码及离线检查不代表运行服务已更新。下一独立闭环为标准EventData收到日期/报告日期的受控维护及本地/GMT一致性；任意现场EventCustomData字段仍为独立部署契约依赖。

验证记录见[本轮汇总](quality-fixtures/event-data-summary.json)。全项目3961项及2项子测试通过，最终代码定向381项通过；两次HTTPX真实网络尝试均0，计数重叠不相加。受限代码/离线验证完成，现场未验收。
