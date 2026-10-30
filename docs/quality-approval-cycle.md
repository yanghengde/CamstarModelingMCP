# C1：完成审批、修订重提与周期追溯

本轮C2.A扩展见[角色替代审批](quality-role-approval.md)：仅原Role2且明确允许、原生当前员工角色/组织核验通过才开放，保持实际签署人；默认C1限制不变，委托及额外电子签名仍未开放。

日期：2026-10-30。本轮在同一审批闭环内开发，当前实现72个质量工具；工具数量不表示业务完成率。统一状态见[闭环交付总清单](quality-completion-plan.md)。本说明覆盖已有、已提交的直接Event/CAPA，原指定审批人，无额外电子签名要求。角色替代、委托、PLM、嵌套流程和复杂事件数据不在C1范围。

## 按业务顺序使用

| 用户意图 | 工具与必要资料 | 核验结果与下一步 |
|---|---|---|
| 查看原对象的审批 | `get_quality_approval_context`：对象、组织，可固定对象ID | 获取原表/条目/决策ID、当前周期和入口；候选不是签署许可 |
| 查看已有模板 | `get_quality_approval_template`：真实模板名称，可固定模板ID | 完整命名审批人、层级及决策；单模板最多100条目、50决策，不读口令 |
| 首次发起完成审批 | `prepare_quality_approval_assignment`：原对象/组织、模板、质量解决代码、处置说明和挂接评论 | 仅无直接审批表的Active对象；模板必须等于组织唯一Close必审映射。**挂接并立即路由**，携带关闭处置资料，不是单独挂表，也不表示对象已关闭 |
| 路由已有空白表 | `prepare_quality_approval_action(action="route")`：原表ID及说明 | 仅无旧周期/旧决策的Pending表；与上述挂接并路由二选一，不能给已路由表重复路由 |
| 原审批人批准／拒绝 | `prepare_quality_approval_decision`：原表/条目/允许决策ID及评论 | 绑定当前员工与原审批人、原生当前条目和Approve512；批准10、拒绝20是决策码，不是表状态码。多级或同级多人按原生条件推进 |
| 取消尚未签批的路由 | `prepare_quality_approval_action(action="cancel")`：原表ID及说明 | 原生表回Pending0，写RoutingCanceled30历史；不等于表状态Cancelled20。已有部分签批明确拒绝，不撤销既有结论 |
| 核对旧周期 | `get_quality_approval_history`：原表ID，可明确`approval_cycle_gmt` | 保留原对象、原条目、审批人、决策、评论和事务ID；展示旧/当前周期，包含取消历史，不自动归因MCP成功 |
| 修订Event资料 | `prepare_quality_event_update`：原表ID、`previous_cycle_gmt`及明确文字变化 | 仅唯一表已拒绝/取消、无待签条目的Active/InReview基础Event。保持报告人、日期、优先级；Load中的审批配置只读，执行不回填它 |
| 修订CAPA资料 | `prepare_capa_approval_revision`：原表ID、旧周期、评论及明确简述/说明变化 | 固定Load保持报告人、发生时间、严重度、优先级、系统性问题；原生会刷新报告时间，必须明确`allow_reported_date_refresh=true`。附加组织、自定义数据、法规报告、交叉引用仍拒绝 |
| 开启新审批周期 | `prepare_quality_approval_action(action="resubmit")`：原表ID、明确`previous_cycle_gmt`及重提说明 | 逐条核对原路由及同周期决策/审批人/评论/完成时间和持久历史指针，再调用原生Route；不删除历史、不改审批人、不自动修改资料 |

每个prepare产生独立、可审阅计划。`execute_quality_transaction`必须确认同一plan_id/digest，只尝试一次；200、400、超时或中断均先保持unknown。`inspect_quality_transaction`只读原对象证据；需要人工最终审计结论后，才能通过`review_quality_transaction`处理占用。**当前同值、空历史、同名审批表或一次HTTP200都不能充当最终审计证据。**

模板存在通知配置时，挂接、表级操作及签批必须明确`allow_notifications=true`；该选项不证明邮件已经成功送达。准备后原对象/模板/条目/组织策略/历史变化，执行前重建计划会拒绝原计划。

## 原生流程与保留边界

- `AssignApprovalSheet`预校验会查找FromCompleted表，创建或复用并清理当前审批；因此首次挂接只接受原对象无直接表，**准备阶段不调用该服务的execute=false或PerformEvent**。仅读源数据和当前Swagger，生成静态计划，明确显示动态权限及动态签名尚未核实；实际单次执行由MES作最终验证。普通确认不替代电子签名。
- 标准模型下`QualityObject.Review`设InReview；Reject只发送拒绝通知，不保证对象回Active。修订允许经独立核实的Active/InReview，始终要求当前Edit动作和原责任人/角色。
- 某一条目拒绝后，原生仍可能继续后续层级，全部处理完才将整表设Rejected。表仍Routed时不允许提前修订/重提，不把Rejected改名为“退回”。
- `ClearApprovals`会清理当前决策、评论、审批人签署结果及完成时间，并重置周期；**不会清除LastDecision指针**。重提须先证明旧决策已持久保存；新周期签批允许保留的旧指针，但必须确认它属于同一对象/表/条目的更早周期。本周期或外对象的指针仍拒绝。
- 普通`prepare_capa(action="update")`遇到已有直接审批表会拒绝，不能绕过专用修订的旧周期及保持字段保护。

历史读取使用固定列和固定SQL，不请求Employee完整实体。单次最多100条表历史和200条签批历史；可指定精确GMT周期，保留7位小数。超限/缺字段/错误父对象/重复ID/重读变化/过大输出均拒绝部分结果，不冒称完整。旧条目从当前表移除后，历史仍按原条目ID展示。此入口不包含所有嵌套流程历史，也不提供C7最终事务自动归因。

## 验证与待验收

连续模拟脚本直接调用正式工具处理函数，覆盖首次挂接并路由、单人/多级/同级多人、拒绝后等待后续层级、Event拒绝/取消后修订重提、CAPA修订刷新报告时间后重提、新周期批准、旧历史保留。每次服务端状态推进和人工最终审计均是**隔离数据中的显式模拟**，不声称运行了Camstar原生引擎或真实人工验收。

证据和脚本：

- [连续Event业务脚本](../tests/test_quality_approval_business_flows.py)、[CAPA修订及连续脚本](../tests/test_quality_capa_approval_revision.py)。
- [挂接与模板](../tests/test_quality_approval_assignment.py)、[周期防护](../tests/test_quality_approval_cycles.py)、[Event修订](../tests/test_quality_approval_revision.py)、[历史](../tests/test_quality_approval_history.py)、[多轮问法](../tests/test_quality_approval_conversation.py)。
- [本轮标准模型发现](quality-fixtures/approval-revision-native.json)、[挂接及路由流程](quality-fixtures/approval-revision-native-flows.json)、[维护动作常量](quality-fixtures/approval-assignment-constants-native.json)、[CAPA原生修改规则](quality-fixtures/approval-capa-revision-native.json)。均为只读标准MDB证据，不能代替部署模型。
- [2026-10-30只读现场契约探测](quality-fixtures/approval-c1-live-contract-probe.json)：AssignApprovalSheet、UpdateCAPA均未取得有效JSON定义。没有创建/路由/签批真实数据，没有发通知、改配置或重启服务。

现场尚须核对：实际Swagger/Load响应及自定义扩展、原生事务权限和动态签名、组织模板映射、报告时间变化、审批层级/拒绝/取消的实际行为、邮件收件人和历史关联。连接未恢复前，不将C1标成已验收，也不把新增工具及模拟脚本当作整个质量模块完成。

本轮验证汇总：[完整证据摘要](quality-fixtures/approval-c1-summary.json)。全项目3437项及2项子测试通过，质量相关2231项；后补场景及最后路由/发现变更另经524项定向验证，均通过。两个离线HTTPX网络守卫均为0次真实请求尝试。定向数与全项目数不能相加。
