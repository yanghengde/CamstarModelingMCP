# T05 投料：契约盘点与意图预览

2026-10-23。阶段部分完成，**未实现 issue_components，不能执行投料**。

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
