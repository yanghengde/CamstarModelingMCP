# T05 投料：契约盘点与意图预览

2026-10-23初始阶段快照；后续模式5执行/四表核实见[单次计划说明](component-issue-plans.md)，2026-10-25模式2来源校验及真实非执行回显见[模式2说明](component-issue-bulk-preview.md)。模式2无执行计划，阶段总验收未完成。

## 契约发现

依据 `Swagger/extensions/Shopfloor-ComponentIssue.json`：

- ComponentIssue 必填 container、serviceDetails；serviceDetails 为 IssueDetails 列表，注释说明需求由 LoadRequirements 加载。
- IssueDetails 必填 qtyRequired；其 qtyIssued 是累计已投减移除数量，不能用来写本次投料数量。
- IssueDetails.actuals 为 IssueActuals 列表；顶层 issueActualDetails 是另一种 IssueActualDetail 列表，二者不能随意混用。
- IssueActualDetail 包含 bomLineItem、fromContainer、fromLot、fromStockPoint、product、qtyIssued；其中 qtyIssued 注释是该明细实际投料数量。
- issueControl：1序列级容器、2批量容器、3外部批号、4非批号库存点、5仅数量、6显示数量但不记录。预览仅允许1–5；这不代表已验收全部模式。
- PerformEvent 的 schema 只有 event/inputData，未提供足够证据证明 LoadRequirements 的调用时序、实际输入及无副作用。未试调用未知事件。

## 已实现

`preview_component_issue`，material 分组，只读。输入目标容器、明确产品版本、有限正数数量、追踪模式及必要来源。产品唯一解析与目标容器身份核对通过后返回 resolved_product、resolved_target 及 intent。

意图预览始终 executable=false、complete=false；不生成 payload，不发 Shopfloor 请求，不声称来源存在、数量足够、物料匹配、单位一致或可以投料。来源目前只记录为意图，未做余额/关联校验。数量模式不允许填入会被忽略的来源。

34项相关测试通过：无效数量/模式/引用在请求前拦截、身份不匹配、结果不可执行、只读策略、既有BOM和能力目录回归。只读现场探测见 [component-issue-preview-validation.json](component-issue-preview-validation.json)，探测参数为合成意图，不是待执行的业务需求。

## 下一次必须继续的步骤

1. 核对现场 ComponentIssue 页面或准确的服务事件说明，确定 LoadRequirements 的实际调用、输入和返回结构。只读方法需先证明其语义，不能因事件名像查询就直接执行。
2. 为具体测试工单/容器找到真实 BOM 需求行，确认 NamedSubentityRef 的父对象、名称与版本定位，以及服务端提供的 issueControl、qtyRequired、UOM。
3. 核对来源容器/批号/库存点、产品版本、余额、单位及工序允许条件；构造完整投料预览，替换当前仅意图模式。
4. 固定预览内容与执行内容的关联，执行前重新读取状态；新增 issue_components 时登记写入、always_confirm，验证聊天与直接MCP入口边界。
5. 核实成功响应和实际消耗历史查询；对超时或响应丢失先标记结果未知，再只读核实，不能自动重放。验证测试样本后才能标记现场事务验收通过。

T05总任务保持未完成。不得把本工具描述为“投料已接入”。部署新预览工具需重启应用并刷新客户端清单；本轮未重启服务。


## 续接：专用需求匹配查询

2026-10-23 新增 `get_component_requirement`（material，只读），并集成到意图预览。实时取得 Query ComponentIssueInquiry Swagger，归档于 `Swagger/extensions/Query-ComponentIssueInquiry.json`。

输入固定为 parentContainer + objectName，execute=true、展开 IssueDetails。objectName 的契约定义为产品或来源容器名称，用来匹配父容器需求中的产品/替代产品。模式1/2预览以来源容器匹配，其他模式以产品名称匹配。返回的是单条 IssueDetails 对象，不是数组，不是完整BOM或实际消耗历史。

现场探测发现：传 container 返回13369346（字段写入被拒），改为契约规定的 parentContainer/objectName 后，测试组合 SN0000001 + WM-900A 返回13501228（不满足任何物料需求）。前者没有被误解释成未匹配。只调用专用 Query inquiry，未调用 Shopfloor PerformEvent 或实际投料。

工具区分 matched、no_matching_requirement、query_failed、requirement_incomplete、unknown。成功响应仍核对父容器名称/ID和匹配对象，缺少BOM行或需求数量不标为完整。非空成功响应的现场回读尚待样本验证；目前只经过模拟测试。即便 matched，也不授予执行许可，意图预览仍 executable=false。

37项相关测试通过。证据：[component-requirement-validation.json](component-requirement-validation.json)，含契约摘要校验。下一步优先取得真实需求匹配样本，核对服务需求行与 IssueActualDetail 的关联；再验证单位、来源状态及消耗历史。LoadRequirements 的事件协议和执行结果核实尚未解决，T05保持进行中。


## 续接：历史回读与样本可用性

2026-10-23 新增 `get_component_issue_history`，固定只读 Query/AdHoc SQL。先核对容器名称及16位十六进制ID，再按 IssueActualsHistory.ToContainerId 读取直接投料历史，保留历史ID、事务ID、来源容器/批号/库存点、产品和数量。offset>=0，limit=1..100，多取一条判断后续页；不接受用户SQL，不拼接容器名称。

字段经 INFORMATION_SCHEMA.COLUMNS 现场核对。当前查询范围内 IssueActualsHistory、ProductMaterialListItem、MfgOrderMaterialListItem 的 COUNT 均为0；真实需求行和非空历史样本不可用。证据：[component-issue-history-contract.json](component-issue-history-contract.json)。首次计数查询的别名 RowCount 被SQL解析器拒绝，已改为逐表 TotalRows 成功查询，保留原失败证据。

40项相关测试通过，包括身份校验、固定SQL、分页、空页和错误目标记录拦截。该工具只展示直接接收容器的原始投料历史，不扣除移除/退料，不代表净消耗；ID排序不是时间排序。空历史不能证明某次请求失败，更不授权重试。非空历史的数量/来源语义仍待现场验收。

执行链剩余输入条件：需要一个已配置物料需求的测试容器/工单、真实来源物料及可用的需求加载操作证据。现环境没有这些样本，不能继续通过反复空查询证明写入正确。下次应提供对应测试对象，或明确授权配置隔离测试数据；没有这些条件时可继续独立的T06契约开发，但T05必须保持未完成。

## 续接：自主创建真实需求样本及非执行结构验证（2026-10-24）

用户已明确委托自主创建测试对象。按Start技能的只读预检，核对产品类型、明确工艺版本、单位EACH、层级BOX及Owner/StartReason唯一选择后，创建并回读确认CDX-T05-20261024-B-COMP/1、TARGET/1、B-MO及B-C01，共2产品/1工单/1容器。仅新建测试对象，未修改原生产产品、工单或工艺。容器Qty=1，物料需求T05-QTY为2 EACH、issueControl=5，不涉及物理来源容器/库存扣减。

创建过程中发现实际契约与旧Swagger差异：ProductEntity没有uom而有StdStartUOM，物料行IsPhantom被拒写；均停批并保留原错误。确认缺失工单后仅修正已诊断输入受控续接；已成功创建的产品和工单没有重发。展开MaterialList时服务将未选择根Product/Qty置null，改用普通读取校验根、展开读取校验行。完整创建/验证/失败记录见[日志](t05-fixtures/CDX-T05-20261024-B.json)和[计划](remaining-development-plan.md)。

真实ComponentIssueInquiry暴露同类投影问题：只expand IssueDetails会使根ParentContainer/ObjectName为空。工具现增加显式select，非空需求已返回matched/complete=true，含目标容器ID、物料产品版本、BOM行ID与所属工单、数量与单位。非法布尔/非有限/负需求数量不再标matched。见[需求验证](t05-fixtures/requirement-tool-validation.json)。

新增scripts/verify_component_issue_contract.py，固定/api/ComponentIssue、execute=false、无事件/执行开关/自动重试。仅模式5、完整明确引用、足够主数量、第二数量明确全0时构造serviceDetails需求与issueActualDetails本次量，不把累计QtyIssued当本次数量。逐项核对目标、BOM行及owner、产品版本、单位、数量与preventOverIssue；Shopfloor的数字/布尔字符串按明确协议解析，不用Python字符串真值猜测。

现场固定非执行检查HTTP200且serialization_verified=true，transaction_verified恒false；前后查询QtyIssued/ActualQtyIssued均0、NetQtyRequired仍2。见[证据](t05-fixtures/component-serialization-verified.json)。需求及回显ValidSequence=false，不能强制true或把系统/ES字段回填绕过验证。LoadRequirements仍不在REST事件列表中；需先明确序列/IssuePoint及需求加载执行语义，再接不可变计划与实际投料入口。**该阶段尚未实现issue_components；后续受限实现见下文及component-issue-plans.md。** 模式1–4、实际消耗和精确历史关联继续留在[续接计划](remaining-development-plan.md)。


## 数量模式预览校验（2026-10-24续接）

preview_component_issue现在对模式5返回quantity_requirement_checks：逐项检查固定产品身份/版本、IssueControl、需求行及所属对象ID、单位、NetQtyRequired和第二需求量。使用服务返回的净剩余量，不以累计QtyIssued自行推算；超需求、错误产品/模式及非零第二需求量标blocked，缺字段/无效数值标unknown。产品替代关系、单位换算、第二数量输入仍未实现。

真实隔离容器2 EACH预览中六项可观测检查通过，assembly_sequence保留unknown，executable始终false，见[现场预览](t05-fixtures/quantity-preview-assessment.json)。契约对ValidSequence的说明是“是否已经完成序列验证”，不能仅凭false断言现场禁止投料，也不能把它强制改成true。IssuePoint/序列规则和需求加载执行语义仍需核实。新增测试覆盖超量、缺失/非有限净剩余量、产品版本与ROR不匹配、无单位/行引用、第二数量及预览不写入。

## 隔离执行契约实验与数量差异规则（2026-10-24续接）

用户已授权继续开发并自主准备测试数据。本次仅针对已归档的B批隔离容器，新增scripts/probe_component_issue_execution.py：默认只读规划；显式--apply才发送标准ComponentIssue execute=true，不填ValidSequence/ValidateQtyDifference，不加跳过/覆盖字段。固定容器ID、产品版本、工单/需求行、未消耗2 EACH与空实际历史均需核对。共享journal和进程锁限制一次业务尝试，取消/超时保留unknown，不重放。

第一条请求错误地select了不存在的TxnId，服务端在OData EnableQueryAttribute.OnActionExecuting阶段返回400。保留原始错误后，脚本只对这一确切URI拒绝增加一次显式--correct-rejected-select，限定仅删除错误select字段、body不变、需求仍未消耗、历史仍为空；对业务失败和超时不允许此续接。随后真正进入业务服务的请求返回HTTP400/ErrorCode13500677：“Issue Difference Reason is required if the actual quantity issued differs from the quantity required.” 该请求的本次意图是1 EACH、需求2 EACH；不能据此证明顶层IssueActualDetails已经被Execute正确处理，更不能认定序列验证通过。

见[执行证据](t05-fixtures/execution-probe-select-corrected.json)和[复核](t05-fixtures/execution-probe-review.json)：前后QtyIssued/ActualQtyIssued仍0、NetQtyRequired仍2，实际历史完整空页，主线历史只见Start。它们是现场观测，不是唯一请求→事务证明。journal继续保留，不进行业务重试、自动解锁或伪称成功；该次实验时issue_components尚未实现；后续模式5受限工具见下文。

预览新增quantity_difference：ValidateQtyDifference必须为明确布尔；为true且本次数量不等于净剩余量时标blocked并说明差异原因尚不支持；字段缺失/错误类型为unknown。完整需求量只是通过这一项意图检查，不能替代Actuals/IssueActualDetails实际执行结构及其他现场规则验收。后续优先核实实际量处理和差异原因选择，再开发通用计划/执行链。

## 模式5嵌套Actuals与正式单次计划（2026-10-24后续）

现已实现prepare_component_issue、get_component_issue_plan、issue_components、recover_component_issue_attempt、resolve_component_issue_outcome，详见[受限执行说明](component-issue-plans.md)。只支持完整未消耗工单需求、固定产品版本、仅主数量、无替代或差异原因。真实嵌套回显逐项检查BOM行/所属工单、产品/版本、单位、主/第二实际量及Actuals目标，不回填累计QtyIssued，不改ValidSequence/校验开关。

本次新建C和D两组隔离样本，各4/4对象创建并回读核实；C用嵌套Actuals进行契约实验，D走正式准备计划→提交前复核→单次执行→证据回读。两组均一次execute=true、HTTP200、实际历史2 EACH、QtyIssued=2、NetQtyRequired=0。没有重复B的失败请求，也未改动原生产对象。见[现场汇总](t05-fixtures/mode5-live-summary.json)及[正式计划结果](t05-fixtures/formal-execution-D.json)。

正式D计划保留unknown和容器占用：服务完成消息及历史观测尚未由通用需求行级因果验证器核实，未自动调用人工核实或冒充human_attestation。C主线TxnId与实际记录TxnId的现场一致性证据已保存，但不存在的BOMLineItemId列不能拿来编造行关联。原计划不可重放，人工/中断恢复仅按各自审计前提使用。其他追踪模式、部分投料、差异原因、序列/IssuePoint非空规则和完整客户端验收继续列于[续接计划](remaining-development-plan.md)。

## 四表需求行级自动核实（2026-10-24后续）

上述unknown为前一阶段快照。已确认真正的需求行历史字段为ProductMaterialListItemId；新增reconcile_component_issue，按唯一Comments标记和历史主线/需求明细/历史头/实际记录四表核对。只有同一事务、目标/工序、需求行、固定产品ID、单位与主/第二数量全部匹配才自动保存verified_committed与审计并解除本计划占用。缺失、重复、错误、读取失败或本地状态变化不判成功/失败，保留unknown。

D真实计划已四表匹配并持久化为verified_committed，未重发投料、未调用人工核实；[证据](t05-fixtures/formal-reconciliation-D.json)。历史头TxnId与主线TxnId含义不同，使用HistoryMainlineId/IntegrationTxnId关联，HistoryId仍为容器ID。新增48项回归覆盖关联、数值、歧义、进程占用和审计回滚；全量885项及2项子测试通过。完整方法和边界见[工具说明](component-issue-plans.md)，其他追踪模式及完整现场验收仍待补。

## 首次部分量及差异原因（2026-10-24后续）

新增get_component_issue_difference_reasons固定上下文只读选择工具，prepare_component_issue增加显式issue_difference_reason；首次部分量必须选明确、唯一且未冻结的原因，需求行/Actuals两层回显与提交前选择值复核都要匹配。自动核实进一步绑定两张历史的IssueDifferenceReasonId，不关闭ValidateQtyDifference、不自动套用默认原因。完整量仍不接受多余原因，已有消耗后补投/第二数量/替代料未支持。

E隔离批5/5对象创建并回读核实，一次标准执行1 EACH（需求2）成功，已投1/净需求1，四表与双原因ID核实为verified_committed；未补投剩余量。18项新增测试及全量903项/2项子测试通过，详见[实现边界与证据](component-issue-plans.md)。

## 模式5已有消耗后的增量补投（2026-10-24后续）

已支持需求=累计已投+净剩余且无待处理实际量、退料及第二数量时的增量补投。本次数量必须正且不超净剩余；小于原需求仍须显式差异原因。原QtyRequired不变，不回填累计QtyIssued/NetQtyRequired，确认展示基线/本次量；旧余额/上下文变化拒绝旧计划。四表历史仍核对新标记及本次量，不把历史总量当本次成功证据。

E另用新计划补投一次1 EACH成功，累计2/净需求0，新事务核实为verified_committed；旧余额计划被拒，未再次投料。本轮无新MES样本，新增28项测试、全量931项与2项子测试通过。退料净额、其他追踪模式及完整现场验收仍待补，详见[执行说明](component-issue-plans.md)。
