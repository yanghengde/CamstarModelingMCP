# Camstar质量API与MCP工具

本轮代码交付收尾完成。交付边界、47个工具核对结果、部署接续及待开发/待验收清单见[阶段交付说明](quality-delivery.md)；不是全部质量功能或MES验收完成。

2026-10-28更新：quality分组共47个工具，新增关联Event已Closed、原NCR仍Open时的质量侧关闭收尾计划。新增既有NCR升级质量事件的组织选项查询和提交计划。新增产品当前采样状态查询及明确选择的计数器/手动检验水平调整计划。已支持检验计划只读配置检查。已支持零采样计数且无有效采样历史批的数量/频率维护、维护历史及原请求核对。既有范围包括AQL/检验水平维护与查表、单容器检验、定义维护、产品关联、建批、批采样、MES已通过批的OK执行及逐点追溯。工具注册、契约验证和模拟测试不代表现场质量事务验收完成。参数见[批维护](quality-lot-maintenance.md)、[AQL与检验水平](quality-sampling-references.md)、[单容器检验](quality-inspection.md)、[定义和建批](quality-definitions-lots.md)、[批采样与整批OK执行](quality-lot-execution.md)、[历史追溯与请求核对](quality-history.md)。

## 实际发现的接口与本轮范围

最新质量侧NCR关闭轮：新增64项测试，全量2087项测试、2项子测试通过，其中质量相关881项，父pytest离线HTTPX真实网络尝试0。新增prepare_quality_ncr_closure，封装NCRCloseQuality，保守范围为原关联Event已Closed且有关闭时间/关闭人、原NCR仍Open、原容器Active。明确NCR解决代码，不推进事件审批或强制关闭，不自动替代普通NCRClose；原报告/原事件/容器/解决代码重查，统一确认单次提交，unknown保留占用。现场Event及已升级NCR为空，真实收尾待验收。见[功能说明](quality-ncr-quality-close.md)及[本轮摘要](quality-fixtures/ncr-quality-close-summary.json)。

最新检验水平复位轮：新增37项测试，全量2023项测试、2项子测试通过，其中质量相关817项，父pytest离线HTTPX真实网络尝试0。扩展现有prepare_current_sampling_status_update的reset_inspection_level参数，不新增工具，总数46。恢复计划默认与命名水平互斥；未请求的计数器不发送，不猜有效默认或切换结果。绑定计划及候选表配置、重读防变化，沿用确认/单次提交/unknown占用。现场无目标非执行回显通过，状态记录仍为空，真实效果待验收。见[功能说明](quality-sampling-reset.md)及[本轮摘要](quality-fixtures/sampling-level-reset-summary.json)。

最新NCR升级轮：新增68项测试，全量1986项测试、2项子测试通过，其中质量相关780项，父pytest离线HTTPX真实网络尝试0。正式NCREscalate请求按组织唯一分类/子分类组合核对，明确优先级，绑定Open原报告、失败代码与原容器；不指定生成事件名称、不覆盖签名或额外调用关闭服务。原报告最终状态待MES验收。同时支持NCR失败码关联的SQL整数文本。现场组织选项查询成功，但已检查组织映射和优先级列表为空，正目标及真实升级仍待验收。见[功能说明](quality-ncr-escalation.md)及[本轮摘要](quality-fixtures/ncr-escalation-summary.json)。

最新当前采样状态轮：新增70项测试，全量1918项测试、2项子测试通过，其中质量相关712项，父pytest离线HTTPX真实网络尝试0。现场连接恢复，核对CurrentSamplingStatusUpdate契约及SamplingCurrentStatus列，新增状态查询和明确1..20条容器采样状态调整计划。计数器不是频率，不改已有任务；不全选、不处理批采样。该首轮未开放复位，后续已扩展。未执行预览返回200，真实状态表仍为空，正目标和真实调整验收待补。见[功能说明](quality-current-sampling.md)及[本轮摘要](quality-fixtures/current-sampling-summary.json)。

2026-10-27新增检验计划只读配置检查，复用既有接口契约；新增67项测试，全量1848项测试、2项子测试通过，质量相关642项，父pytest离线HTTPX真实网络尝试0。测试和边界见[配置检查说明](quality-configuration-check.md)及[本轮摘要](quality-fixtures/configuration-audit-summary.json)。该配置检查轮现场连接超时，未新增现场契约验证或MES业务写入；后续当前采样状态轮已恢复并补齐接口，见上文。

最新计数分组轮扩展两个现有检验工具，工具总数保持41；修正显式子字段选择、规范拒收原因子明细及未公开CDOTypeId的处理。新增57项测试，全量1781项测试、2项子测试通过，质量相关575项，离线HTTPX真实网络尝试为0。多组实际累计和完整目标回显仍待现场验收，见[计数分组说明](quality-counted-groups.md)及[本轮摘要](quality-fixtures/counted-groups-summary.json)。

最新批维护轮回归：1724项测试、2项子测试通过，质量相关518项，离线HTTPX真实网络尝试为0；新增83项维护与历史核对测试。UpdateSamplingLot无目标非执行探测返回200，四条只读SQL通过，真实抽样批仍为空；未验证真实维护及任务重算。见[本轮摘要](quality-fixtures/lot-maintenance-summary.json)。

最新AQL/检验水平轮回归：1641项测试、2项子测试通过，质量相关435项，离线HTTPX真实网络尝试为0；新增69项配置维护与查表测试。两个Modeling GET/select/expand及两个ChangeCount查询和字符容量查询已现场只读验证，配置列表仍为空。见[本轮摘要](quality-fixtures/sampling-reference-summary.json)。

最新历史核对轮回归：1572 项测试、2 项子测试通过，质量相关 366 项，离线 HTTPX 真实网络尝试为 0。现场 5 条新增固定 SQL 和 2 条非空历史检查通过；历史仍为空，父键映射和最终事务因果链尚待现场验收。见[本轮摘要](quality-fixtures/quality-history-summary.json)。

| 业务 | 当前部署发现的API | 本轮处理 |
|---|---|---|
| NCR不合格报告 | NCRCreate、NCRUpdate、NCRClose、NCREscalate、NCRCloseQuality；GetNCRs、NCRHistoryInquiry | 创建/更新/关闭及按组织唯一分类映射升级质量事件计划、当前记录及历史；NCRCloseQuality已增加受限的已关闭事件后收尾入口，真实写入待验收 |
| CAPA | CreateCAPA、UpdateCAPA、CloseQualityObject | 固定类别5的创建/更新和Close处置关闭计划；草稿、提交与关闭申请分别处理，仍由MES校验审批/签名 |
| 质量对象/事件 | QualityObjectInquiry、QualityObjectHistoryInquiry、OpenQualityObject、CloseQualityObject、TriageQualityObject | 有界列表、对象详情与历史；通用开启/关闭/分派仍待验证 |
| 检验/抽样 | SamplingPlans、SampleTests、SampleDataPoints、InspectionLevels；CollectSamplingData、CollectLotSamplingData、CreateSamplingLot、ExecuteSamplingLot、UpdateSamplingLot | 定义维护、产品关联、单容器及批计数/测量计划、建批、批历史及MES已完成通过批的OK执行计划；整批返工/报废、自动扣量/全检转换仍未开放 |
| SPC | SPCControls、SPCChartDefs、RecordSPCViolation、InlineSPCTxn等 | 控制/控制图定义查询；不计算判异、不记录违例或自动放行 |
| 不良/返工/冻结 | ContainerDefect、Rework、Hold、Release | 保留已有工具名，归入quality并确认后执行；不是新版NCR/CAPA journal流程 |
| 报废 | PartScrap、ScrapReasons | PartScrap要求resource和scrapQty，不能据此推出Container报废；未新增scrap_container |

首轮读取了52份相关Swagger，以及Shopfloor/Query EDM和SQL列元数据；2026-10-26又核对定义CRUD、建批契约及MfgLot存储。发现记录：[首轮目录](quality-fixtures/inventory.json)、[首轮契约](quality-fixtures/contracts.json)、[EDM](quality-fixtures/metadata.json)、[表字段](quality-fixtures/table-columns.json)、[本轮契约](quality-fixtures/definition-lot-contracts.json)。Swagger存在只说明接口被发布，不能证明当前账号有执行许可、模块流程配置完整或已验收。

## 47个工具

| 工具 | 用途 |
|---|---|
| prepare_quality_ncr_closure | 原关联Event已Closed且原NCR仍Open时的NCRCloseQuality收尾计划，明确NCR解决代码，不替代事件关闭/审批或普通NCRClose |
| get_ncr_escalation_options | 指定组织完整分类/子分类组合及签名设置，不自动选首行，不证明目标许可 |
| prepare_ncr_escalation | Open且未关联升级事件的原NCR升级计划；唯一组织组合、明确优先级、原容器及回显核对，统一确认单次执行 |
| get_quality_api_inventory | 已发现API、已封装工具和未开放范围；有核查日期，不是实时健康检查 |
| get_current_sampling_status | 固定产品版本当前状态分页，真实ID、当前频率与独立计数器、检验水平和批归属 |
| prepare_current_sampling_status_update | 明确选择容器采样状态，准备计数器、命名检验水平或原生恢复计划默认，不全选、不猜有效默认、不改已有任务，统一确认单次提交 |
| check_sampling_plan_configuration | 固定计划版本的测试/数值点及AQL-检验水平配置诊断，区分问题与工具限制，不替代运行时任务或质量判定 |
| prepare_sampling_lot_update | 全量核对原批、成员、零计数任务及有效采样历史，准备数量/频率调整，保留其他选项 |
| get_sampling_lot_maintenance_history | 按历史批ID查询UpdateSamplingLot维护值、完整评论、事务及冲销状态，非最终成功证明 |
| get_sampling_reference_definition | AQL等级或检验水平完整有界表及变更计数，不加版本或Status |
| prepare_sampling_reference_definition | 两类命名配置及明细的创建/更新计划，闭区间与显式整数样本量校验，统一确认单次提交 |
| preview_sampling_table_lookup | 批量区间→代码→AQL行的只读配置查表，保留数量及阈值，不代表MES最终要求或质量判定 |
| get_inspection_result_history | 逐样本/逐点历史候选关联，最多50样本100点；父键映射仍待非空现场验收 |
| find_quality_execution_candidates | 原计划摘要、环境和实际尝试记录约束下，查精确评论与历史目标对应的候选事务 |
| reconcile_quality_execution | 核对明确候选与原检验/批采样/整批OK请求；只读、不解锁，字段匹配不是最终成功 |
| list_quality_references | 固定白名单的失败/原因/解决代码、组织分类、优先级、处置、抽检/自动冻结/SPC配置，limit≤100 |
| get_quality_reference_details | 配置详情；抽检计划/测试必须固定版本、ID或显式ROR；展开受控明细 |
| list_nonconformance_reports | 有界NCR列表，支持经身份核对的容器、原始状态筛选；多取一行判断后页 |
| get_nonconformance_report | 当前NCR记录及完整失败代码名称、ID和顺序；读取期间根记录变化则失败 |
| list_quality_objects | QualityObject视图有界列表，返回真实Id/Type/Category/Status，不虚构名称 |
| get_quality_object_details | 固定QualityObjectInquiry读取状态、分类、责任人和流程/审批详情 |
| get_quality_closure_options | 实时查询CloseQualityObject的ResolutionAction候选，保留服务端原码；不证明目标许可 |
| get_quality_history | NCR或质量对象的固定Inquiry历史，起始行从1开始，不把分页/空页当成完整历史或提交结论 |
| get_container_quality_context | 当前冻结计数、质量控制/抽检标志及有界ContainerSampleData；无质量/放行结论 |
| get_inspection_test_definition | 固定版本测试、完整检验点及数值限/单位/拒收原因/副作用配置 |
| get_container_inspection_tasks | 当前持久化检验任务、要求/已采数及固定测试版本，不补造任务 |
| prepare_container_inspection | 单容器计数/数值测量本次结果准备，完整样本回显；确认后复用质量事务单次提交 |
| get_container_inspection_history | 容器检验历史汇总原始字段，不推断本次增量或最终成功，不包含逐点历史 |
| prepare_inspection_definition | 数值检验点、计数/测量测试、抽样计划的创建/更新/当前ROR升版；仅静态维护预览 |
| prepare_product_sampling_plan | 固定产品版本直接关联固定计划版本，不重建现有容器任务 |
| get_sampling_lot_details | 抽样批及成员/样本独立分页，验证类型和父ID，不作批质量判定 |
| prepare_sampling_lot_create | 明确产品版本、数量和单位，核对CreateSamplingLot非执行回显，不开工或挂接容器 |
| prepare_lot_inspection | 从明确成员记录本次计数/测量结果，核对整批任务累计数，不代进站，不用单容器计数替代 |
| get_sampling_lot_readiness | 完整有界批、任务及成员前置核对，不把本地满足条件当成MES通过或业务许可 |
| prepare_sampling_lot_completion | 仅MES已完成且通过批的OK码1执行计划；核对全部成员，不开放返工2/报废3 |
| get_sampling_lot_history | 按历史SamplingLotId关联采样汇总和来源，不作最终因果核实 |
| prepare_nonconformance_report | NCR创建/更新/关闭的只读准备与execute=false回显验证 |
| prepare_capa | 类别5的CAPA创建/更新准备；明确组织、简述、草稿/提交 |
| prepare_capa_closure | 类别5 CAPA的Close处置关闭准备；明确组织、QualityResolutionCode及关闭说明，不覆盖审批/签名 |
| get_quality_transaction_plan | 不可变计划、请求、原始执行响应、过期和审计状态 |
| execute_quality_transaction | 确认、重建、比较摘要及关联占用、原子领取，按白名单单次执行Shopfloor或Modeling请求 |
| inspect_quality_transaction | 当前记录和有限历史作人工证据，不认定本次成功，不解除占用 |
| review_quality_transaction | 有提交记录的本机中断恢复或人工最终证据审计；不写MES，不重放原计划 |
| container_defect / rework | 已有不良登记和返工直接封装；需确认，现场写入验收待补 |
| hold_container / release_container | 已有Container冻结/解冻，不是工单释放；需确认，现场写入验收待补 |

质量工具已接入MCP注册、Agent分发、能力目录、load_tool_group(group="quality")、自然语言路由和确认展示。示例：“查询NCR历史”“查看CAPA质量对象”“查看当前抽检状态”“查询SPC控制配置”。查询不暴露质量执行工具；加载写工具也不代表取得写入授权。

GetNCRs在当前元数据中没有分页参数，因此列表采用部署字段已核对的固定只读SQL，而非无界拉取再伪称完整。所有SQL表/列/排序固定，名称用Unicode字面量转义，ID与分页严格检查；工具不接受任意SQL。

## NCR计划示例

```json
{
  "action": "create",
  "report_name": "NCR-DEMO-001",
  "container_name": "TEST-SN-001",
  "failure_codes": ["明确存在的失败代码"],
  "failure_type_name": "明确存在的失败类型",
  "ncr_comment": "发现问题的说明"
}
```

以上传给prepare_nonconformance_report，只生成计划。名称、容器、失败代码均须来自用户和实际查询，不自动编造测试对象或挑选默认原因。示例不是本轮实际创建的数据。

- create调用NCRCreate：container与ncrFailureCodes在根级。
- update调用NCRUpdate：nonconformanceReport在根级，完整失败码列表位于serviceDetail.ncrFailureCodes。省略failure_codes时，从已核实关联表读取完整现有清单并保留；明确传入时执行完整替换，不能将新增单个代码误作追加。关联为空、重复、不完整或超过50条时，不自动补全。省略清单且没有任何明确变更时不生成计划。
- close调用NCRClose：serviceDetail.ncrResolutionCode必填，不把关闭简单实现成改状态数字。
- 调查/纠正/预防说明分别放在failureInvestigationComments / correctiveActionComments / preventiveActionComments的comments子字段，不能传为同名裸字符串或客户端dirty字段。
- NCRStatus保留原码；枚举成员名称存在不等于已验证数字映射，不据此猜测已关闭、已解决或质量合格。
- 不处理组件缺陷明细、升级到质量事件、NCRCloseQuality、电子签名或流程覆盖。

失败代码关联使用部署SQL表NonconformanceReportNCRFailure和NCRFailureCode，按已核实EDM字段NCRFailureCodes的FieldId=10019筛选，校验父ID、代码ID、名称、顺序和唯一性。读取关联前后核对根记录；计划还绑定关联及代码ChangeCount，执行前变化则拒绝提交。名称和ID均在预览中展示。现场目前没有非空NCR，因此已验证查询结构和模拟异常，非空现场关联仍待验收。

## CAPA计划示例

```json
{
  "action": "create",
  "capa_name": "CAPA-DEMO-001",
  "organization_name": "明确存在的组织",
  "brief_description": "问题简述",
  "description": "详细说明",
  "is_systemic_issue": false,
  "occurrence_date": "2026-10-25T10:30:00+08:00",
  "submit": false
}
```

以上传给prepare_capa。类别固定5，不把Event/CAR/NCR都套进CAPA接口。组织必须明确，创建必须有简述。支持分类/子分类、优先级及带时区发生时间；false和空字符串不会被误当成未提供。

submit=false准备保存草稿；true申请提交，仍由MES流程、权限和签名规则校验，不表示批准、完成CAPA或关闭质量对象。普通更新不能改变组织，拒绝已知冻结、关闭/删除或类别不符的对象。不开放通用原始body_json，不填写密码/签名、内部dirty字段或绕过流程标志。

仅修改分类或仅submit=true时，也显式加载ServiceDetail验证CAPADetail类型；不因请求没有serviceDetail字段而跳过类型校验或误拒合法准备。

## CAPA关闭计划

先用get_quality_closure_options读取实时处置候选，用get_quality_object_details核对目标，再向prepare_capa_closure传入：

```json
{
  "capa_name": "明确存在的CAPA名称",
  "organization_name": "该CAPA当前组织",
  "quality_resolution_code_name": "明确存在的质量解决代码",
  "close_description": "关闭依据及有效性检查说明"
}
```

现场返回Close=1、AttachToCAPA=2、CreateCAPA=3。工具每次根据实时Name=Close解析原码，并将名称、说明和所有候选绑定计划；不由模型猜数字。当前只支持Close（无需后续行动）处置，不支持挂接或创建另一CAPA，也不把普通NCR解决代码当作QualityResolutionCode。

工具同时核对CAPA固定表记录、QualityObjectInquiry和CloseQualityObject的execute=false状态回显，要求组织一致，拒绝冻结、关闭/删除或未知状态。Inquiry仅绑定目标和业务/审批字段，排除临时Inquiry/明细的InstanceId及Parent，避免重复查询让计划无故失效。预览保留审批上下文和关闭说明；请求不设置approvalRequired、ignoreProcessModel、processESignatures或签名字段。执行仍使用execute_quality_transaction，经确认后重新读取并比对状态、审批上下文、处置候选和引用，再单次提交。关闭申请可能需要MES审批，工具不将HTTP回执或当前状态归因为关闭成功。

关闭与更新共用同一CAPA占用；unknown期间不能改用另一动作，已知目标ID相同的改名也不能绕过占用。当前没有可用于关闭预览的现场CAPA，完整目标回显、真实关闭、审批及签名仍待现场验收；已完成实时候选查询、契约核对和模拟流程验证。

## 计划与结果语义

计划有效1小时，摘要绑定部署环境、命令参数、当前记录、引用ID、容器快照和已验证回显。执行前重建并逐项比较，随后在SQLite事务中领取；同名同类型对象或已知相同目标ID有in_flight/unknown时，其他计划不能绕过占用。原计划一旦尝试执行，就不能再用。

现场验证发现质量服务将布尔/整数回显成字符串，同时每次非执行加载会生成临时ServiceDetail/Parent ID。工具只接受明确等价的标量回显，忽略服务临时ID，保存请求字段、明细类型及稳定的Reporter/Initiator默认引用；重复预览摘要一致。默认责任人也会展示在确认内容里。

HTTP响应成功或“Update completed successfully”不代表本次质量事务已被历史核实。即使execute=false也可能出现该文字。因此执行结果统一先unknown，保存原响应和尝试记录，不自动重试。inspect只收集证据，同名、同值、当前状态或空历史都不能自动解除占用。

当前尚未接入经非空现场样本验证的请求标记到最终质量历史链，最终结论只能由人工核对后review(action="resolve")记录committed/not_committed；verified_success仍为false，明确属于人工结论。recover只在原执行进程锁已释放、存在尝试记录时把in_flight转unknown，继续占用，不证明服务端结束。审计和状态在同一SQLite事务保存，原响应/恢复链保留。

新增库为data/quality_plans.sqlite3；同机进程锁和本地数据库需要所有写入进程统一使用。原有不良/返工/冻结/解冻直接工具未迁入这个journal，不能假定它们具有同样的恢复/核实语义，也不能把它们用于重试NCR/CAPA未知提交。

## 本轮验证与明确未完成项

- 第五轮（2026-10-26）：**1501项测试、2项子测试通过**，质量专项295项；新增68项，离线HTTPX真实网络尝试0。覆盖计数/测量批采样→未知占用→人工最终证据审计→整批OK执行的MCP模拟会话。[摘要](quality-fixtures/lot-execution-summary.json)、[JUnit](quality-fixtures/lot-execution-junit.xml)、[现场只读证据](quality-fixtures/lot-execution-read-validation.json)。当前无SamplingLot样本，完整目标预览、真实事务、继承/派生计数及后续影响仍待验收；未写MES。
- 第四轮（2026-10-26）：**1433项测试、2项子测试通过**，质量专项227项；新增48项，离线HTTPX真实网络尝试0。[摘要](quality-fixtures/definition-lot-summary.json)、[JUnit](quality-fixtures/definition-lot-junit.xml)、[只读和静态准备](quality-fixtures/definition-lot-read-validation.json)。Modeling没有execute=false回显；静态准备与重建摘要核对通过后已取消本地测试计划，目标未创建。非空维护、完整建批预览和真实写入仍待验收。
- 第三轮最终全量回归：**1385项测试、2项子测试通过**，质量专项179项；较第二轮新增86项。离线HTTPX真实网络尝试0，覆盖MCP准备/确认策略/单次提交/未知核对模拟会话；[检验摘要](quality-fixtures/inspection-summary.json)、[JUnit](quality-fixtures/inspection-junit.xml)、[现场只读结果](quality-fixtures/inspection-read-validation.json)。当前没有非空现场检验样本，真实执行和最终历史链仍待验收。
- 第二轮最终全量回归：**1299项测试、2项子测试通过**，包含质量专项93项；较首轮新增52项，覆盖保留/替换失败码、关联完整性、关闭处置、审批/组织状态变化、临时ID、防重及仅根字段更新。父pytest离线HTTPX真实网络尝试0：[JUnit](quality-fixtures/phase2-junit.xml)、[输出](quality-fixtures/phase2-test-output.txt)、[网络](quality-fixtures/phase2-network.json)、[本轮摘要](quality-fixtures/phase2-summary.json)。
- 首轮全量回归：**1247项测试、2项子测试通过**，父pytest离线HTTPX真实网络尝试0：[JUnit](quality-fixtures/final-junit.xml)、[输出](quality-fixtures/final-test-output.txt)、[网络](quality-fixtures/final-network.json)。其中首轮新增41项用例覆盖身份、分页、字段层级、回显、过期、状态变化、防重、取消、恢复及审计回滚。
- 现场只读：参考数据、NCR/质量对象固定有界查询和SN0000001质量/采样字段均可读取：[证据](quality-fixtures/read-validation.json)。当前NCR失败代码、NCR记录、质量对象、抽检计划和SPC图定义样本为空；不能据此声称相关流程业务配置已齐备。
- CAPA execute=false完整准备通过，重复预览摘要一致；准备计划已在本地取消，确认没有创建目标CAPA：[预览](quality-fixtures/capa-preview-validation.json)、[原始回显](quality-fixtures/capa-preview-echo.json)、[回查及注册](quality-fixtures/registration-and-postcheck.json)。
- NCR创建/更新/关闭和CAPA真实提交、非空详情/历史、账号权限、流程审批、电子签名及客户端多轮联合验收仍待完成；本轮没有创建、更新或关闭真实质量记录。
- 第二轮现场只读证据：[关闭动作及新增契约/列定义](quality-fixtures/phase2-contracts.json)、[关联查询与21个工具注册](quality-fixtures/phase2-read-validation.json)。关联查询使用不存在的固定ID，只验证SQL结构，不证明非空业务正确。没有创建、更新或关闭真实质量对象。
- 第三轮新增单容器计数/测量检验工具，详见[检验说明](quality-inspection.md)。现场测试定义及任务样本为空，完整目标预览和真实提交仍待验收；不能将模拟测试或无目标预览报错称为验收完成。
- CAPA仅开放Close处置的关闭准备；整批仅开放MES已完成且通过后的OK执行计划。通用质量对象开启/关闭、挂接/创建CAPA、流程分派、批返工/报废、自动扣量/事件、SPC计算/判异和通用质量放行仍未开放。
- 运行服务未重启，新增工具需要部署重启并刷新客户端清单后生效。
