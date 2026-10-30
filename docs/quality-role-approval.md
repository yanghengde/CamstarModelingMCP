# C2.A：按角色授权的替代审批

2026-10-30。C1受限审批链已有代码和离线验证，现场仍受连接阻塞。本轮按用户“根据进度继续开发”的指示，推进C2中的角色授权闭环，不将C1标成已验收。该角色授权交付时为72个质量工具；后续C2.C新增一次转派入口后为73个，详见统一清单。

这里的“当前员工”指MES/API会话实际返回的Employee，不从聊天者身份推断。确认计划前须核对展示的实际员工；共享服务账号不会自动变成聊天者本人。

本轮入口是已有有效的直接Event/CAPA完成审批模板或审批表，条目原配置允许角色替代（SubstituteOption=2）；出口是合法当前员工完成批准／拒绝、按原策略修订重提并能在历史中区分原审批人与实际签署人。仅支持没有额外电子签名要求的基础对象，委托任务和口令签名仍未开放。

## 使用顺序

1. `get_quality_approval_context`读取原对象、组织、表、当前周期、条目和允许决策。`substitute_policy=role_substitute`只是配置，不代表当前员工有权签署。
2. 模板读取、首次挂接并路由、已有表路由、取消及重提，若原表含Role2条目，必须明确`allow_role_substitution=true`。省略仍按C1指定人模式处理，不修改SubstituteOption、原审批人或角色；不把Named1改成Role2。
3. `prepare_quality_approval_decision`同样必须明确该选项。只允许原生会话返回的当前Employee、原生当前EntryForApproval、Routed表/条目、有效层级、原列表决策和Approve512。
4. 原生授权检查通过后展示原审批人、实际员工、原角色/组织及授权依据。审阅同一计划后由`execute_quality_transaction`确认并单次提交，不接收Employee、User、签署口令、认证头或他人身份覆盖。
5. 若整表已拒绝或原生取消，Event/CAPA文字修订及`resubmit`须继续明确`allow_role_substitution=true`、原表ID和旧周期。修订仍要求原责任人/角色及Edit权限；角色审批资格不授予修改事件资料的权限。CAPA刷新报告时间的独立允许选项仍必须明确。
6. `get_quality_approval_history`同时保留`ApproverId`（原审批人）和`ApprovedById`（实际签署人）。旧周期决策、评论及完成时间不能被当前新周期覆盖。unknown仍需人工最终事务证据，不按某个签署人或当前Approved状态自动确认成功。

涉及的已有入口：

| 入口 | 明确允许Role2后增加的行为 |
|---|---|
| `get_quality_approval_template` | 读取包含Role2或Named1/Role2混合条目的有效模板；不授予当前员工签署资格 |
| `prepare_quality_approval_assignment` | 保持模板原策略挂接并立即路由；仍是静态计划，不调用危险原生预览 |
| `prepare_quality_approval_action` | 保持原策略route/cancel/resubmit；已有签批仍不能普通取消，重提前逐条保护旧历史 |
| `get_quality_event_update_context`、`prepare_quality_event_update` | 保持原Role2审批配置，仅处理允许的事件文字资料 |
| `prepare_capa_approval_revision` | 保持原Role2策略和原明细；原生报告时间刷新仍须另行允许 |
| `prepare_quality_approval_decision` | 对Role2核实实际当前员工的原生角色/组织授权，再以该员工实际身份审批 |

混合表中Named1条目始终要求实际员工等于原审批人；即使选了允许角色替代，也不能由其他成员处理Named1条目。DelegationTask非空、未知替代策略、冻结对象/员工/角色、未轮到的条目及额外签名要求均阻断。

## 原生依据与授权核验

标准`SignApproval.Validate`仅在Role2时跳过Employee必须等于原Approver的检查，但仍调用`Employee.CheckRoleAndOrg`。该方法执行`ValidateEmployeeRoleOrgCombination`，其SQL Server逻辑为：

```sql
SELECT COUNT(*) AS RowCount
FROM csiAuthGetAllowedUsers(?Org, ?Role)
WHERE EmployeeId = ?Employee
```

实现使用同一原生函数和相同参数顺序，固定原组织名、原角色名及服务返回的实际员工ID。名称使用固定SQL字面量转义，不接受任意SQL、函数名或调用方指定员工。

不直接把EmployeeRole中的某条匹配记录当授权结果，因为Camstar原生函数还负责组织继承/传播等规则。读取当前员工完整角色快照（最多100条）和冻结/传播/版本字段作为变更依据，权限结论仍取原生函数。函数不存在、定义不可见、计数未知或为0、角色清单不完整时拒绝，不回退到猜测。

授权函数只在数据库端计算定义SHA-256，计划仅保存函数ID及摘要，不读取完整SQL到模型。准备期间双重核对，执行前重建全部快照；函数定义、实际员工、角色配置、原组织/角色或通知/签名策略变化时，拒绝原计划。当前部署须具有相关只读查询及函数定义核验权限，未修改任何数据库权限。

标准模型证据：[角色/员工与原生校验](quality-fixtures/approval-c2-authorization-native.json)、[原生授权查询及EmployeeRole字段](quality-fixtures/approval-c2-role-membership-native.json)。来源为只读标准MDB，不等于当前部署已验收。

## 会话和凭据边界

- 角色审批预览使用从现有API配置取得的请求凭据及当时有效Cookie；提交复用执行前最后一次核验实际员工的同一份内存会话副本，不再临时读取另一套凭据或共享Cookie。
- 若预览响应建立或刷新Cookie，停止准备且不生成可执行计划；不自动重试，不把新旧会话混合。此阶段要求稳定会话，后续须以现场行为验证兼容性。
- 凭据和Cookie不进入工具参数、计划正文、摘要、日志或事务库；也不保存它们的指纹。计划仅保存实际员工身份及公开业务授权依据。临时内存副本不是电子签名输入通道。
- 角色审批执行仅保存尝试、HTTP状态及异常类别，不保留可能反射会话信息的原始响应／异常正文；标记`response_capture=omitted_for_role_session_privacy`。这会减少本地错误详情，最终证据需核对MES原对象/原周期历史，不用会话信息充当审计证据。
- 服务端并发变更仍由MES最终校验，本地快照不提供跨系统事务隔离。响应、当前员工或授权通过都不等于本次审批成功；结果始终先unknown。

## 验证与未完成内容

离线连续脚本覆盖Role2模板挂接、拒绝或未签批取消、原责任人修订、按原策略重提、另一有权员工批准及旧/新周期实际签署人历史；另覆盖Named1/Role2混合表。所有MES状态推进和人工审计是隔离fixture中的明确模拟，不是现场原生引擎执行。

同时核对CAPA提交状态的布尔序列化：`false`、`0`及未知值不能因字符串非空被当作已提交。

负向验证包括无授权、函数定义不可核实、角色清单缺失/重复/超限、员工/角色/组织或授权版本变化、会话切换、Cookie刷新、委托/签名要求、重复提交与超时/中断；验证凭据及Cookie不会出现在计划、工具输出和临时事务库中。

- [授权与执行保护用例](../tests/test_quality_role_approval.py)
- [完整角色周期脚本](../tests/test_quality_role_business_flows.py)
- [会话复制及刷新阻断](../tests/test_quality_role_transport.py)
- [当前只读通道复核](quality-fixtures/approval-c1-channel-diagnostic.json)：当前环境连接及直接连接均为ConnectError，未执行真实审批、通知、部署或重启。

C2整体仍未完成：受控电子签名输入／一次性引用／签名失效恢复、原生委托与复杂授权、真实部署契约和业务验收均保留待办。下一闭环是C2.B安全签名输入与提交，不允许把密码放入模型可见参数或用普通确认代替签名。

交付验证：[本轮汇总](quality-fixtures/approval-c2-summary.json)。完整回归3500项及2项子测试通过，质量相关2294项；最后49项角色/会话验证通过（含后补HTTPX Cookie合并用例），离线网络守卫均为0次真实HTTPX尝试。本轮代码闭环完成，现场验收仍未完成。


后续C2.C新增[条目一次转派及受委托签批](quality-approval-delegation.md)，走Named1及独立原生转派历史；不能与allow_role_substitution同时选用。日期任务、连续转委托仍拒绝，角色门禁不因新增功能而放宽。
