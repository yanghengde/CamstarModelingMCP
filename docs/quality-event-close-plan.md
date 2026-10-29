# 基础Event受限关闭计划

2026-10-29新增 `prepare_quality_event_closure`，该阶段质量分组61个工具；后续新增[创建资料核验](quality-event-creation.md)，当前68个工具。在[关闭前置诊断](quality-event-closure.md)之后，提供原生CloseQualityObject受限准备和统一确认执行。真实MES关闭、最终历史及部署副作用仍待验收。

## 参数与顺序

| 参数 | 规则 |
|---|---|
| event_name、organization_name | 明确原事件名称和组织，不自动选择目标 |
| quality_resolution_code_name | 明确质量解决代码，使用QualityResolutionCodes，不混用NCR解决代码 |
| close_description | 必填非空关闭说明，最多4000个UTF-16代码单元 |
| event_instance_id | 可选原持久ID；名称及ID必须一致 |
| comments | 可选事务备注，最多200个UTF-16代码单元；附加本地生成的请求标记 |

先调用 `get_quality_event_closure_context` 查看阻塞和未核实项。诊断complete不表示可关闭，也不产生执行计划。用户明确请求关闭后，调用 `prepare_quality_event_closure`，核对原事件、原ID、组织、质量解决代码、关闭说明、原生Close处置原码和账号上下文。工具不能接受电子签名、审批状态或IgnoreProcessModel输入。

通过现有 `execute_quality_transaction(plan_id, plan_digest)` 的系统确认后，仅提交同一计划一次。结果先为unknown；`inspect_quality_transaction`只读原ID及最多20条质量历史，最后由 `review_quality_transaction`人工审阅最终事务证据。当前Closed状态、空历史和HTTP200均不能证明本次关闭成功，不自动解锁或重试，不追随同名新事件。

## 支持范围

仅限基础Event7489、Active=1、已提交、已首次分流、未冻结，已有有效原责任人、角色、优先级和报告人。保持事件原资料和归属，不修改CAR策略。

必须没有CAR要求、流程及原流程模板、检查表或已保存检查表、关闭时间、自定义EventData、直接审批表、法规报告及任何关联NCR。该范围刻意排除NCR联动、CAR关联评估、批次处置和复杂流程；不是所有部署关闭事件的通用业务规则。复杂事件仍可调用只读诊断，但不能从这个准备入口执行。

复用已支持字段的完整持久事件读取，前后核对变更计数。组织、责任人、角色、优先级和质量解决代码按持久ID、名称、冻结及变更计数核实；读取人员时仅选择安全身份字段，不持久保存凭据。组织全局签名及关闭事务7712签名要求均拒绝，其他事务签名清单不误判为关闭签名。

运行时读取当前CloseQualityObject Swagger，核实根引用与关闭参数类型；Close处置候选来自原生RequestSelectionValues，必须唯一，不硬编码处置数值。准备调用 `POST /api/CloseQualityObject?execute=false`，核对完整目标、组织、解决代码、说明和备注回显。

原生可用动作必须含Resolve位64，当前UserIsOwner及UserHasRole为true，Employee与原责任人持久ID一致。状态、类别、组织、责任人、角色、优先级、分流和CAR状态须与持久记录一致；流程、完成审批、当前审批及签名上下文明确信息为空，ApprovalRequired和IgnoreProcessModel必须为false。未知或不匹配都拒绝，不覆盖这些服务端标志。

预览之后重新读取原事件、参考配置、审批/法规报告/关联NCR和质量解决代码。执行前重建同一计划并比较摘要；源状态、资料、组织策略、代码或账号上下文变化时，原计划失效。预览中的临时服务/状态明细ID不进入稳定快照。

## 执行与联动依据

原生服务CloseQualityObject类型7712、Resolve动作64分别见[关闭字段目录](quality-fixtures/event-close-native-index.json)和[动作元数据](quality-fixtures/event-triage-native-actions.json)。[本轮保存及校验逻辑](quality-fixtures/event-close-plan-native.json)显示StartModifyingEntity设置Closed状态、关闭时间及关闭员工，若EventData存在NonconformanceNumber可能调用CloseMfgNCR，并可能执行检查表；本入口排除这些数据和检查表。

[原生关闭校验](quality-fixtures/event-close-native-execution.json)检查CAR/CAPA关联、流程及关联批次；[批次检查和质量解决代码列](quality-fixtures/event-close-plan-extra.json)提供进一步依据。失败的探索名称和字段读取保留原错误，不能作为成功发现的API。

标准csi MDB源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不证明当前部署自定义一致。通过非执行预览也不证明所有动态规则已运行或最终关闭已验收。

执行器只发送一条CloseQualityObject请求，不额外发送NCRCloseQuality、ChangeQty或通知。计划使用原事件ID，与首次分流和文字维护共享占用；结果不确定时继续保留占用，当前记录相符不自动归因成功。全局质量日志的占用不代表与所有外部系统或其他日志协调。

## 本轮证据与未完成范围

测试见[汇总](quality-fixtures/event-close-plan-summary.json)、[针对性JUnit](quality-fixtures/event-close-plan-focused-junit.xml)、[完整JUnit](quality-fixtures/event-close-plan-junit.xml)及[离线HTTPX守卫](quality-fixtures/event-close-plan-network.json)。模拟接口和临时计划库验证参数/权限阻断、状态变化、引用冻结、原生回显、确认、单次提交、取消/超时后的unknown及原ID检查；不等同现场业务验收，守卫不是操作系统级网络隔离。

[本轮现场只读检查](quality-fixtures/event-close-plan-live-readcheck.json)只读取必要元数据和Swagger，没有选择真实目标预览或提交业务。本轮没有MES写入、通知或服务重启。部署契约、真实目标关闭、原生历史关联和自定义副作用仍需现场验收。

事件创建、开启、重新分派、CAR关联复杂关闭、批次/NCR联动、审批和电子签名仍见[后续计划](quality-advanced-roadmap.md)。本轮不表示质量模块全部完成。

本轮新增62项测试；针对性389项通过；全量2948项测试、2项子测试通过，其中质量相关1742项，父pytest真实HTTPX网络尝试为0。[工具参数与问法核对](quality-fixtures/event-close-plan-manifest.json)确认61个质量工具均注册且可分发，新增参数模型拒绝额外字段，确认入口保持不变。真实质量计划库只读检查仍为2条cancelled，没有新增现场执行记录。

后续扩展：显式allow_complex_context=true可核实CAR/CAPA关联、流程Stage快照、关联NCR及Approved40审批，原生最终验证流程完成；默认false保持原受限范围。现有关联NCR不等于执行NCR子关闭，仍拒绝EventData/检查表/待审批/签名或需启动审批模板的关闭。见[生命周期扩展](quality-event-lifecycle.md)。

2026-10-29后续扩展：[制造NCR原生联动关闭](quality-event-ncr-linked.md)支持显式双选项、标准类别3事件数据、唯一原NCR及同容器处置完整覆盖；只发父关闭请求，不补发NCRCloseQuality。实际复合事务仍待验收。
