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
