# T05 仅数量投料的单次计划与执行

更新：2026-10-25（Asia/Shanghai）。本页描述模式5的受限执行闭环，不代表所有投料模式及现场规则已验收。

最新模式2已通过prepare_bulk_component_issue接入同计划库和issue_components/reconcile_component_issue/恢复入口，按快照显式模式分派。模式5目标与模式2源/目标发生交集时共享占用限制；旧模式5快照无需重写摘要。完整说明和G现场验收见[共用单次计划](component-issue-shared-plans.md)。

## 已验证的请求结构

先前顶层IssueActualDetails的非执行回显并不足以证明业务执行。新隔离样本C使用ServiceDetails内的ActualQtyIssued/ActualQty2Issued和Actuals列表，完整需求2 EACH，标准execute=true返回HTTP200与完成消息。随后Query需求QtyIssued变2、NetQtyRequired变0，IssueActualsHistory出现2 EACH实际记录。主线历史保存了本次唯一Comments标记，且通过单条固定只读查询核对主线TxnId与实际记录TxnId均为400000000000f0f0。

证据：[嵌套回显](t05-fixtures/nested-actuals-preview-C-selected.json)、[标准执行及前后读](t05-fixtures/nested-actuals-execution-C.json)、[主线事务关联](t05-fixtures/nested-actuals-mainline-transaction-C.json)。这证明该隔离场景的请求路径，不证明所有IssuePoint/序列、差异原因、来源扣减或业务规则。

IssueActualsHistory.HistoryId在此样本等于容器ID，不能误当HistoryMainlineId。尝试读取IssueHistoryDetail.BOMLineItemId被拒（无此列），错误保留于nested-actuals-line-proof-C.json；不得据此虚构需求行历史关联。

## 工具与范围

| 工具 | 行为 |
|---|---|
| prepare_component_issue | 容器、产品名/固定版本、本次数量和可选明确容器ID；只读核对及execute=false回显，生成1小时单次计划 |
| get_component_issue_plan | 查询不可变请求、摘要、期限、状态及持久化结果 |
| issue_components | 确认计划ID/摘要后重读，核对未变化，原子领取并单次execute=true，不自动重试 |
| recover_component_issue_attempt | 受确认保护的同机中断恢复；取得进程锁且有尝试审计才把in_flight转unknown，保留容器占用 |
| resolve_component_issue_outcome | 人工核对最终服务端事务/审计证据并确认后，保存committed/not_committed人工终态，只解除本计划占用，不重开原计划 |
| reconcile_component_issue | 固定只读MES查询完整四表历史链；匹配才审计更改本地成功终态并解除本计划占用，无MES写入或事务重试 |
| get_component_issue_difference_reasons | 只读查询首次部分投料的当前差异原因，不自动选择；完整列表最多100条，明确冻结状态 |

当前仅支持活动、在制容器，需求行属于当前工单，固定产品身份和版本，模式5，正主数量不超过净剩余，第二数量与待处理实际量明确全0、退料记录明确为空。累计已投/净剩余/原需求必须平衡；已有消耗时可规划明确增量的补投，具体见下文。凡本次量小于原需求，都须明确issue_difference_reason名称并重查当前选择值；本次量等于原需求不接受多余原因。仅数量模式不扣来源库存。其他追踪模式、退料净额、替代料、第二数量及标准BOM跨对象需求均拒绝，不猜测或自动转换。

需求行中的累计QtyIssued不回填为本次数量；本次量放入ActualQtyIssued和嵌套Actuals。保留preventOverIssue=true，不填写ValidSequence/ValidateQtyDifference或系统历史字段，不关闭校验。需求查询的瞬态InstanceID不参与摘要；其余约束、产品/工单/工序/步骤/单位/当前数量和活动时间参与复核。唯一审计Comments标记同样受摘要约束。

## 状态及恢复边界

SQLite领取与attempt记录同事务，提交全过程持有本机进程锁；领取前再检查存储和期限。同一容器在本计划库有in_flight/unknown时其他投料计划拒绝执行；本机锁不声称协调其他客户端或其他业务库，跨客户端最终规则由MES执行。已使用计划永不重放。

请求超时、拒绝、异常或取消先持久化unknown，取消后继续传播取消。HTTP200和当前数量不能直接解锁；可随后调用reconcile_component_issue核对下述完整历史链，匹配才自动保存verified_committed。证据不完整时仍unknown，人工核实必须检查最终服务端证据；不能凭空历史推断失败。恢复只释放本地僵持状态，不证明服务端结束。审计、原始结果及人工/恢复链原子保存；仅支持同机数据库、完整锁文件和升级执行进程。

Agent目录、按需加载、中文投料路由和确认展示已接入；执行、恢复、核实均always_confirm。直接MCP客户端须提供同等确认流程，工具不将自然语言或文档内容当授权。

prepare_component_issue的MCP数量参数要求严格的有限正数，拒绝布尔值、数字字符串、零和负数；不依赖框架把字符串或布尔值自动转换为浮点数。直接函数调用继续由同一业务数量检查保护。

## 测试与后续

新增测试覆盖真实回显的逐项核对、非法/部分/超量、第二数量/模式/差异规则、回显引用及ROR冲突，默认只规划、状态变化、取消/超时、journal防重放，以及计划摘要/标记/环境篡改、过期、并发、跨计划占用、人工核实和受锁恢复。领取前存储变化、审计插入失败回滚也有保护测试。全量837项测试和2项子测试通过，离线HTTPX请求0，git diff --check通过。

D正式工具计划实测亦收到HTTP200完成消息，回读实际历史2 EACH、累计2/净需求0；该阶段先保留unknown。后续四表自动核实已将其转verified_committed（见下文），没有调用人工核实工具冒充人工结论。完整结果及证据见[续接计划](remaining-development-plan.md)。已有运行服务未在本轮重启，部署后须重启并刷新客户端工具清单；测试已核对新Python进程的注册和分发。

下一步：正式客户端交互、退料/移除后净额、模式1–4、完整来源/余额规则，以及其他现场版本的事务链验证。T05总验收仍未完成。

## 首次部分投料与明确差异原因（2026-10-24续接）

首次部分投料支持正数量小于尚未消耗的完整工单需求；不支持已有消耗后的剩余补投、超量或替代料。先调用get_component_issue_difference_reasons，固定POST ComponentIssue/RequestSelectionValues、selectionValuesExpression=ServiceDetails.IssueDifferenceReason、最多100条且要求RecordCount/RowCount/实际条数一致。缺失/截断、重复ID、冻结状态未知均拒绝，不自动挑选唯一值或默认原因。

prepare_component_issue新增可选issue_difference_reason名称。部分量必须明确指定当前上下文中唯一且未冻结的原因；原因名/ID写入摘要、重读参数以及需求行和Actuals两层IssueDifferenceReason。提交前重查选择值，冻结、身份变化、摘要变化或两层回显不一致拒绝执行。完整量不接受会被忽略的多余原因。工具确认展示实际请求及所选原因，不修改ValidateQtyDifference或其他校验开关。

自动核实部分量时，除既有四表链外，还要求IssueHistoryDetail与IssueActualsHistory的IssueDifferenceReasonId均等于计划原因ID；QtyRequired保持原需求量，ActualQtyIssued/实际Qty匹配本次数量，不能把原需求覆盖成本次数量。服务拒绝/结果不明不重试；核实原事务不证明当前净余额。

当前默认原因Replace Component没有自动套用。本次按用户隔离数据授权创建CDX-T05-20261024-E-PARTIAL，描述明确TEST DATA ONLY，以及E批2产品/1工单/1容器；各对象逐项回读，生产对象未修改。实际选择值包含默认和测试两个原因，计划显式选择测试原因；非执行回显两层原因匹配。证据：[原因创建](t05-fixtures/partial-reason-creation-E.json)、[上下文选择](t05-fixtures/partial-reason-selection-E.json)、[计划及回显](t05-fixtures/partial-plan-snapshot-E.json)。执行与核实证据、全量测试见[续接计划](remaining-development-plan.md)。

E正式计划a020dd91fbd04188ac15bdfa75e5506b仅执行一次并收到HTTP200，回读QtyIssued=1、NetQtyRequired=1、实际记录1 EACH。四表核实又确认需求明细/实际记录的IssueDifferenceReasonId都为0019c8800000001b，已保存verified_committed并解除该计划占用，原计划不可重放。见[实际执行](t05-fixtures/partial-formal-execution-E.json)和[自动核实](t05-fixtures/partial-formal-reconciliation-E.json)。剩余1没有自动补投，已有消耗后的计划仍拒绝。

新增18项测试覆盖选择值完整性/冻结/重复、两层原因回显、提交前原因变化、存储篡改及部分量历史双原因核对；全量903项与2项子测试通过，离线HTTPX请求0、git diff --check通过。本轮5个隔离对象全部创建并核实，无失败/跳过/未尝试；服务进程未重启，部署须重启并刷新工具清单。

## 已有消耗后的模式5增量补投（2026-10-24后续）

前述“首次”限制为当时实现快照。现可对已有正累计量规划本次增量，但严格要求QtyRequired = QtyIssued + NetQtyRequired、ActualQtyIssued=0，所有第二数量为0，RemovalHistoryDetails明确空列表。数值采用有限非负JSON数及Decimal等式核对，不猜缺字段、不自动舍入补平；有退料/移除、待处理实际量、超原需求或不一致余额均拒绝。

本次数量须正且不超当前NetQtyRequired。原QtyRequired保留不变，QtyIssued/NetQtyRequired不写入请求，实际量仅放ActualQtyIssued及Actuals[].QtyIssued；新请求使用新计划/新唯一标记，旧计划不能重放。即便本次填满剩余量，若本次量仍小于原QtyRequired，也须显式差异原因并双层回显/历史核对，不能因“补齐剩余”省略现场差异规则。

prepare_component_issue及确认展示新增quantity_context：原需求、累计已投基线、净剩余、本次量和预测提交后值；预测不替代服务结果或历史。完整约束/活动时间/历史引用仍绑定原计划，提交前重读，余额被其他事务改变则拒绝旧计划。新“补投”中文路由会加载投料工具，查询补投历史仍只加载只读工具。

E既有测试容器基线为原需求2 EACH、累计1、净剩余1；补投计划请求仍为原需求2、本次1，非执行回显通过。另保存同一基线的第二份计划，用于检验完成后旧计划不会再次投料。原首次部分投料记录及已核实终态保留，补投是新的单次事务，不新建MES样本或修改原因配置。证据：[补投前需求](t05-fixtures/topup-requirement-before-E.json)、[新计划及回显](t05-fixtures/topup-plan-snapshot-E.json)。实际执行、自动核实及旧计划拒绝见[续接计划](remaining-development-plan.md)。

E补投标准请求仅一次HTTP200，新增实际历史0019e68000000005、事务400000000000f10d，本次实际量1 EACH；回读累计QtyIssued=2、NetQtyRequired=0。新计划33b2963a7af548e28aac496fc9c82613四表核实为verified_committed，两层原因均匹配。旧基线计划再次提交在重读阶段被拒submitted=false，未发送投料请求；其本地ready记录仍保留，但当前余额/活动上下文无法满足原摘要，不能通过复核。见[执行](t05-fixtures/topup-formal-execution-E.json)、[核实](t05-fixtures/topup-formal-reconciliation-E.json)、[旧计划拒绝](t05-fixtures/topup-stale-rejection-E.json)、[汇总](t05-fixtures/topup-live-summary-E.json)。

新增28项测试覆盖增量与累计量区分、超净剩余、缺失/负数/非有限余额、待处理实际量、退料历史、确认展示、余额变化、同容器未知计划占用、新旧标记及历史单次量核实和中文补投路由。全量931项与2项子测试通过，离线HTTPX请求0，git diff --check通过。本轮无新MES对象、无生产配置改动；仅在既有隔离容器补投一次。

## 四表历史链自动核实（2026-10-24续接）

对D真实历史按明确ID只读发现，IssueHistoryDetail使用ProductMaterialListItemId关联物料需求行，不能使用先前被拒的BOMLineItemId。ComponentIssueHistory.TxnId在此部署是服务实例ID，与主线TxnId不同；历史头通过HistoryMainlineId和IntegrationTxnId指向主线。需求明细和实际记录的HistoryId则等于容器ID，不能当作主线ID。

reconcile_component_issue只接受具有受本机锁保护尝试记录的unknown计划。确认展示原请求、唯一Comments标记与解除占用条件；工具策略为本地update/always_confirm，因为它可以修改本地终态。MES只有固定SELECT TOP 2只读查询，不执行投料，也不接受自由SQL：

1. HistoryMainline：同容器且Comments等于原计划唯一标记，恰好一条；校验ComponentIssue类型、原工序/操作ID及明确事务ID。
2. IssueHistoryDetail：该事务恰好一条需求明细；校验ProductMaterialListItemId、产品固定版本ID、单位、模式5、需求量/实际量及第二数量0；IntegrationTxnId须指向上述主线。
3. ComponentIssueHistory：明细所属历史头唯一，HistoryMainlineId/IntegrationTxnId均指向上述主线、HistoryId指向目标容器。不能比较头TxnId与主线TxnId是否相等。
4. IssueActualsHistory：该事务恰好一条实际记录，明细ID、目标、产品及主/第二数量均匹配，来源容器/批号/库存点字段完整且为空，符合仅数量模式。

原请求还须能从已绑定需求约束重新构造，不能含额外实际量或偏离该窄范围结构。四表链都匹配才在同SQLite事务内写receipt_checks、reviews与verified_committed结果，保存原unknown结果及证据并解除本计划占用；其他unknown计划不变，原计划永不重放。无结果、多记录、错误引用/数量、权限/格式/超时或核实期间存储变化均不释放占用、不认定失败。核实过程也持有同机进程锁，活跃提交不能被核实/解除。

receipt_checks保存重复核对的证据；审计插入失败整笔回滚。本核实证明原事务曾提交，不证明当前净消耗（后续退料/移除须另查），也不以当前数量或完成消息推断因果。

契约与现场只读证据：[明细字段发现](t05-fixtures/issue-line-history-discovery-D.json)、[关联字段核对](t05-fixtures/issue-receipt-contract-D.json)、[完整链校验](t05-fixtures/issue-receipt-verification-D.json)。新增测试覆盖每条关联及数值、空/重复/错误、标记注入、请求内部不一致、并发/状态/尝试限制、原结果保留、其他计划占用、审计失败回滚和确认展示。

最终全量885项测试和2项子测试通过（离线真实HTTPX请求0，git diff --check通过）。[D实际自动核实](t05-fixtures/formal-reconciliation-D.json)执行4次只读历史查询后，将计划从unknown转verified_committed，verified_success=true、locks_released=true、original_plan_reusable=false，原结果和自动核实证据同事务保存。事务400000000000f0f7、需求行001b7b8000000004、实际历史0019e68000000003全部绑定。没有再次投料、创建MES对象或修改生产对象；B/C实验记录未改变。

## 进程故障与MCP客户端隔离集成（2026-10-25）

新增真实子进程强制终止/响应后崩溃、跨进程防重、HTTP响应丢失、晚到/空/重复/403历史、SQLite最终保存失败，以及新进程stdio SDK和认证HTTP客户端的完整计划→执行→核实→回读/拒绝重放测试。OS锁、SQLite及MCP协议真实运行，MES上下文/历史基于D归档样本，预览/完成响应由测试传输生成；仍不能替代真实MES故障或聊天确认界面验收。

修复MCP数量参数自动转换的问题，布尔/数字字符串/零/负数在预览前拒绝。另修复归档回放测试对本机.env地址的依赖，明确绑定样本环境，并增测不同部署拒绝执行及核实；未放宽生产环境摘要绑定，未修改.env。新增17项测试，最终全量948项及2项子测试通过，父pytest HTTPX网络尝试0，子进程自行阻断默认HTTPX网络传输。没有MES写入或服务重启，新参数校验须在服务重启后加载。首轮环境差异失败原样保留，详见[本轮报告](t05-fixtures/fault-acceptance-2026-10-25.md)。
