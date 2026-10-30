# C2.B通用多签与协签：NCR更新的条件接入

2026-10-30。已实现多人安全输入、要求及角色核验、受控提交适配器和异常恢复代码，离线模拟通过。**尚不能认定通用多签业务闭环完成或现场可用**：现有保存的标准REST契约没有暴露具体口令采集子类型，现场通道仍未贯通；协签原生参数传递还有待核实项。

## 原生发现与本轮入口

QualityTxn覆盖ShopFloor的ValidateElectronicSignatures及ProcessElectronicSignatures，只检查/执行QualityESigDetail。因此SignApproval不能通过附加ESigDetails数组获得通用多签能力；上一轮的单人质量签名保留原范围。

NCRTxn继承ShopFloor的通用签名流程，并从Factory.ESigTxnMap解析要求。本轮以**Open、未关闭、未升级、原容器Active且无冻结的NCRUpdate**为首次接入入口；不扩展NCR创建/关闭、事件审批、委托、批事务或其它签名事务。

标准通用流程为：LoadESigDetails汇合主/附加要求→每项ESigReqDetail生成ESigServiceDetail→按SignatureCount采集CaptureDetails→原生校验要求、人数、同要求内主签署人唯一性→执行采集明细→记录ESigHistorySummary/Detail。ESigPasswordCapture是ESigCaptureDetail的子类型，包含SignerPassword/CosignerPassword。PasswordVerification原码为1。

授权使用员工**ESigRoleGroup.ResolvedEntries**，与C2.A审批的EmployeeRole/组织授权不同。无协签时核对主签署人Role；有协签时核对CosignerRole、不同员工及明确CosignReason，不擅自要求主签署人具备协签替代的原角色。本轮仍要求主签署人与协签人均亲自输入本人有效口令并确认；原生允许的“无主签署人口令、由协签替代认证”等分支不开放。

## 工具与多轮顺序

没有新增MCP工具，质量工具仍为72个。

1. “查看NCR X更新需要哪些电子签名。” → `get_nonconformance_report(name='X', include_native_signature_context=true)`。以验证证书的HTTPS读取固定NCRUpdate Swagger；仅固定LoadESigDetails事件契约核实后加载原生要求，不实际更新、不采集口令、不创建计划，ready_for_execution始终false。契约缺项直接报告。
2. 用户依据原要求明确每个主签署人的分配，以及需要的协签人和原因。调用 `prepare_nonconformance_report(action='update', report_name='X', …, require_native_esign=true, native_signature_assignments=[…])`。
3. 每条分配只含 `requirement_detail_id`、`signer_name`、可选 `cosigner_name`、`cosign_reason_name`。列表是完整分配，不自动选择人员或补足数量，逐项精确匹配SignatureCount；同一要求内主签署员工不能重复。输入模型封闭，没有口令/认证头/执行员工覆盖参数。
4. 以下输入/提交链路是隔离夹具验证过的条件适配设计，当前生产门禁关闭，不返回多签计划或输入链接。只有实际契约及原生认证映射都核实并完成后续代码接入后，才可启用生成原计划和独立本机输入链接的路径。页面显示拟修改内容、执行员工、摘要及每个签名位置的姓名/ID、岗位、要求、签名含义。每位指定人员在本机亲自输入并确认，模型不代填；当前仅支持相关人员现场共同完成同一个页面，不提供远程签名链接分发。
5. 整组输入必须完整且每位同意才原子暂存，最多5次主签署、10个口令位置。没有部分成功；300秒挑战、采集后最多120秒、仅进程内存、一次性领取、过期/撤销/重启清除。单人口令不能充当整组输入，整组输入不能作为单人口令使用。captured不是口令认证或签批成功。
6. 确认同一计划后，`execute_quality_transaction`领取并消耗整组输入，重读原报告/失败码、要求、签名含义、员工角色组、原因及原生实际执行员工，核验同次会话，再单次提交NCRUpdate。口令仅临时注入各自ESigPasswordCapture，清除载荷及缓冲；响应和异常正文不落库/回显。
7. HTTP200/400、超时、中断均不能自动成功或重试。领取后核验失败也销毁整组输入；未知/in_flight占用保留，不能重新采集或换工具绕过。inspect按原容器、服务、请求标记以及HistoryMainline→ESigHistorySummary→ESigHistoryDetail的精确父键/HistoryId/TxnId提供候选，保留原要求及实际主签署/协签/原因ID；不自动证明本次NCR事务成功，仍需最终人工审计。

## 当前契约阻塞与服务端工作单

保存的 `contracts.json` 中Shopfloor/NCRUpdate只声明 `ESigServiceDetail.captureDetails → ESigCaptureDetail`，基础类型无SignerPassword/CosignerPassword且additionalProperties=false，也没有本适配器所需的完整PerformEvent输入定义。**不能往基础类型添加口令、使用未核实的类型判别字段，或改用顶层User/Password覆盖身份。**

当前条件适配器要求现场明确暴露：NCRUpdate的eSigDetails指向ESigServiceDetail；其captureDetails直接指向具体ESigPasswordCapture；该类型声明可写的两项口令及原生引用字段；固定PerformEvent事件输入对应NCRUpdate和eventName。无法满足时保持阻断。即使满足上述字段契约，仍有独立的native_execution_support认证映射门禁；当前固定返回未验证，无环境变量、工具参数或用户选项可绕过。正向单元用例显式替换该门禁为隔离夹具，不能作为生产启用依据。这是条件适配契约，**测试中构造该契约不代表标准服务已提供它**。若实际服务使用其它多态格式，须先获取正式服务契约并据此另行实现，不能更改测试冒充兼容。

后端后续工作须完成以下可验收结果：

- 获取实际部署的NCRUpdate Swagger/服务版本，确认具体子类型如何创建和映射；必要时由服务端提供正式、受认证且验证证书的类型适配能力。当前仓库没有对应MES REST服务端源代码，本轮未修改外部服务/MDB或部署配置。
- 核实主签署/协签口令进入原生VerifyPasswordSignature的完整参数链。所读标准ESigPasswordCapture.Validate调用只明确传Signer、Password、Cosigner，而Shopfloor_VerifyPasswordSignature还使用CSPassword及ESigReqDetail；其隐式参数来源尚未证明，不能据此声称协签验证有效。必须以现场正式服务/原生追踪和正反向业务验证解决；不得由MCP绕过或本地比较口令。
- 实际测试正确/错误主签署口令、正确/错误协签口令、无角色/冻结账号、不同签名要求/人数、重复员工、同人主协签、原因无效，以及拒绝后是否原子回滚、身份是否恢复到原登录用户。
- 核实一次NCR更新、全部签名历史、实际操作员工/主签署人/协签人、通知或其它副作用和最终事务关联；完成前仍保持unknown人工审计，不启动C7自动解锁。

以上是下一步必须闭合的外部接口/原生验收项。C2.C委托可作为独立后续代码闭环推进，但不能将本通用签名范围标为已验收或用委托代替它。

## 证据与验证

标准MDB SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。原生依据：[类型/继承](quality-fixtures/approval-c2b-multi-index.json)、[服务及采集明细](quality-fixtures/approval-c2b-multi-details.json)、[多签流程](quality-fixtures/approval-c2b-multi-flows.json)、[口令校验流程](quality-fixtures/approval-c2b-multi-password-flow.json)、[方法覆盖](quality-fixtures/approval-c2b-multi-method-bindings.json)、[历史列](quality-fixtures/approval-c2b-multi-history-column-list.json)。标准模型不能代替实际部署契约。

离线验证使用虚构员工、明确标注的条件契约、模拟原生响应和临时SQLite。覆盖双主签署及协签、角色组/要求变化、数量/重复/同人/原因错误、原契约缺项、整组失效与一次性、错误/超时结果、载荷清理及不落库/回显、历史候选和工具封闭参数。Edge浏览器使用虚构页面与口令验证桌面/移动布局、三个独立位置、逐人确认、清空及取消；不调用MES或模型。

汇总：[本轮交付记录](quality-fixtures/approval-c2b-multi-summary.json)、[定向测试](quality-fixtures/approval-c2b-multi-final-junit.xml)、[完整回归](quality-fixtures/approval-c2b-multi-full-junit.xml)、[浏览器](quality-fixtures/approval-c2b-multi-ui.json)。HTTPX离线守卫不是操作系统级隔离。本轮未实际MES更新、签名、通知、部署或重启现用服务。
