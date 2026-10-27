# MCP 工具覆盖与加载评估

> 此文为最初 107 工具审查快照，不代表当前未完成清单。按需加载与基础详情已实施，见 [工具加载说明](tool-loading-and-details.md)。后续新增高级工艺结构、产品工艺链和历史兜底查询，当前 117 工具；最新验收与剩余样本限制见 [高优先级增强](process-context-enhancement.md)。

检查日期：2026-10-22。范围：当前代码注册表、现有 Swagger，以及配置环境的 Modeling、Shopfloor、Query 实时 Swagger 和 Modeling `$metadata`。本次未新增业务工具、未执行候选业务事务。

## 结论

当前注册 107 个工具。Workflow、Spec、Operation、Product、MfgOrder 的已核实基础实体端点已基本映射，主要缺口是关联详情、结构化领域查询以及 Container 的更多业务事务。Step 属于 Workflow 子实体，不应另外虚构独立 `/api/Steps`。

应区分三类：

1. 已有可靠专用工具，例如 Workflow 步骤读取。
2. 有通用入口但缺少专用封装，例如 ContainerInfoInquiry 可通过现有 Query 工具调用；不等于完全没有能力。
3. 服务存在而未接入工具，例如 Shopfloor Hold/Release/Split；Swagger 存在不等于业务参数、权限和状态约束已经验证。

## 按对象盘点

| 对象 | 已有工具与能力 | 建议补齐 | 契约证据与边界 |
|---|---|---|---|
| Workflow | 16 个：基础读写、计数、选值，以及步骤新增/修改/删除、连接、重建 | Gates、分支条件、返工路径、路径修改/删除、子 Workflow 递归与循环检测、引用关系检查 | 实时导航有 Gates、Steps；现有步骤写入集中在 SpecStep，基础 body_json 可容纳高级字段，但无专用验证 |
| Step | 2 个专用读取工具：全部步骤、指定步骤；写入工具归在 Workflow 模块 | PathSelectors、ReworkPaths、ReworkPathSelectors、SchedulingDetail；子 Workflow 类型核对后再递归 | 实时 StepChanges 明确声明这些导航；当前只读普通 Paths，不能宣称已覆盖返工/条件分支 |
| Spec | 11 个：列表、详情、增删改、计数、选值 | 关联 Operation/ResourceGroup/Setup/ElectronicProcedure/DocumentSet 的完整展示；TxnMap、标签、电子签名、计时器等配置明细 | 实时 SpecEntity 声明引用及 TxnMap、LabelTxnMap、ESigTxnMap、StartTimerTxnMap、EndTimerTxnMap 等导航 |
| Operation | 10 个：列表、详情、创建/更新/删除、计数、选值 | DisallowedTxns、SchedulingDetail、WorkCenter、DispatchRule、原因组引用及反查使用它的 Spec | Operations 的实时 Swagger 未声明 PATCH，不能为了凑齐 CRUD 就新增 patch_operation |
| Product | 11 个：基础读写、计数、选值 | 产品→Workflow→Steps→Spec→Operation 链；BOM/BillOfProcess、Attributes、替代品、版本关系 | Workflow/BOM/BillOfProcess 为引用，Attributes 为导航；创建/更新可通过 body_json 设置额外字段，缺的是稳定的业务封装 |
| MfgOrder | 11 个：基础读写、计数、选值；创建参数已包括产品、数量、状态、日期、产线等 | MaterialList、Attributes、已执行任务；工单→Container/WIP/生产进度查询 | 有 MaterialList、MfgOrderExecutedTask 等导航，Query 有 MfgOrderInquiry。现有 patch_mfgorder 在本次实时 Swagger 中没有 PATCH 契约，需标记待验证，不能按已验证能力使用 |
| Container | 7 个：Start 与其选值、Move/MoveIn/MoveOut、Defect、Rework；另有 ContainerLevel/NumberingRule 辅助工具 | 专用详情/状态/历史查询；Hold/Release、Split/Combine、Associate/Disassociate、ChangeQty、Close、MoveNonStd、物料消耗、采集数据 | Query 与 Shopfloor 必须分开；查询可复用现有 Inquiry 通道，事务需独立接入并验证 |

“基础端点已映射”只表示工具与契约对应，不表示每一个写入路径已在当前生产状态下试运行。

## 已确认存在、尚无专用封装的 Container / 工单接口

本次读取了下列分组的 Swagger，未调用业务 POST/PUT：

| 服务 | 分组 | 用途/后续建议 |
|---|---|---|
| Query | ContainerInfoInquiry | 封装容器详情，核验输入字段后组合产品、工单、当前路线/步骤等返回信息 |
| Query | ViewContainerStatus | 封装状态查询 |
| Query | ContainerHistoryInquiry | 封装历史查询，验证时间范围、分页与结果完整性 |
| Query | MfgOrderInquiry | 封装工单执行信息，具体产量/关联容器字段需进一步验证 |
| Shopfloor | Hold / Release | 挂起与释放，配合原因码和当前状态校验 |
| Shopfloor | Split / Combine | 拆分与合并，校验数量、目标容器和层级 |
| Shopfloor | Associate / Disassociate | 容器关联与解关联，校验父子关系 |
| Shopfloor | ChangeQty / Close | 数量调整与关闭；不要把 ChangeQty 直接等同于已验证的报废流程 |
| Shopfloor | MoveNonStd | 非标准移站，需目标路线/步骤与状态验证 |
| Shopfloor | ComponentIssue | 物料消耗/发料事务，需验证明细与跟踪字段 |
| Shopfloor | CollectData | 数据采集，需核验定义及数据点约束 |
| Shopfloor | ContainerMaint | 容器维护，范围大，优先封装明确场景，暂不开放任意字段/事件执行 |

同一事务 Swagger 中的 PerformEvent、AdvancedPerformEvent、PUT 和选值端点不应机械地各变成一个模型工具。

## 关联对象候选

实时 Modeling 目录还确认存在以下分组，但本次仅发现目录，未逐个验证其业务接口：

- BOMs / ERPBOMs / ResourceBOMs：产品与用料。
- Resources / ResourceGroups / ResourceFamilies：Spec 与执行资源。
- DataCollectionDefs / DataPointCollectionDefs / UserDataCollectionDefs：数据采集定义。
- HoldReasons / ReleaseReasons / ReworkReasons / ScrapReasons / QtyAdjustReasons：事务原因码。
- ProductFamilies：产品分类。
- MfgOrderProcedures / MfgOrderTaskLists / MfgOrderTaskStatuses：工单任务与过程。

按实际需求优先增加只读查询；不要一口气注册每个对象全部维护操作。

## 当前加载成本与问题

`agent/llm_client.py::register_tools` 将全部 MCP 工具转成 function schema。LangGraph 和 legacy 模式每次模型请求都发送全量列表，无领域选择或按需加载。

- 实测 107 个定义序列化后合计 72,666 字符（按模块列表序列化求和，约 72.7k 字符，不是 token 数）。
- 当前配置模型名为 deepseek-flash。本次未验证该模型/服务的工具数上限或计费规则，不套用其他供应商限制。
- 多轮推理重复传递这些定义，增加请求体和上下文占用；是否命中供应商缓存以及实际计费需单独测量。
- 多个同义入口增加选择歧义：全项目有 7 类对象各 3 个 `_by_odata_key` 变体，共 21 个；可保留 MCP 兼容性但从默认模型工具集隐藏。隐藏后为 86 个，仍应按领域筛选。
- 描述过长、body_json 无明确字段约束、通用错误处理和错误 key 示例都会影响准确性，工具数量不是唯一原因。
- 通用 `smart_response` 大结果只保留 KEY_FIELDS，可能丢掉关联字段/导航结果。Workflow Step 已用专用输出规避，其余领域详情需要同样处理完整性。
- 性能日志主要记录调用耗时，未见模型 usage/token、候选工具数量及工具选择错误率记录，不能仅凭现状断言耗时会降低多少。

## 扩容前应先处理的执行策略缺口

现有 `classify_tool` 依赖函数名前缀及少量名单，以下写入操作目前返回 None（作为未分类处理）：

- add_spec_step_to_workflow
- connect_workflow_steps
- import_workflows_from_excel

因此这几项不会按真实写入计入当前 mutation 计数；Excel 导入内部还有批量创建/重建，外层一次调用不等于只影响一条数据。

建议显式工具元数据记录 read/write/delete、作用对象、预期影响量、允许环境与确认策略。注册时校验，未知工具不能默认当只读。直接 MCP 调用与聊天 Agent 的确认层也应核对，不能假设经过同一策略。路由筛选只影响模型看到什么，执行入口仍需校验已注册/已授权工具。

## 建议加载方案

服务端继续注册全部可用工具，模型每轮只接收当前任务需要的定义：

1. 保留少量通用发现、对象定位工具。
2. LangGraph 增加工具选择节点，按任务选择领域工具包。
3. 查询优先加载读取工具；有明确修改意图再加载必要的写入工具，确认策略独立执行。
4. 查询产品工艺时允许跨 Product、Workflow、Step、Spec、Operation 组合；查询生产进度时允许跨 MfgOrder、Container、Query 组合。
5. 不命中时允许受控扩展候选包，不应永久锁死在首次关键词分类。
6. 将 Swagger 与可用选值缓存为后端元数据，避免每轮把整个 Swagger 注入上下文。

建议初始每轮候选规模以 12–25 个为实验目标，不是供应商上限。用典型任务测量首次响应、总耗时、token usage、错误工具选择、完整查询率，再调整。

建议工具包：建模查询、建模修改、Container 查询、Container 执行、质量/物料、Excel 导入。不要把所有能力塞进一个不受约束的“任意 REST 请求”工具。

## 实施优先级

- P0：显式执行策略、工具选择节点、默认隐藏重复入口；大响应完整性标记；核查不在实时 Swagger 中的现有 PATCH。
- P1：Spec/Product/Operation/MfgOrder 的关联详情；Container 详情、状态、历史与工单查询，复用 Query 通道。
- P2：根据使用频率接入 Hold/Release、非标准移站、Split/Combine、关联/解关联及原因码。
- P3：BOM/物料、采集数据、工单任务、复杂分支与返工配置。

建议的 `get_spec_details`、`get_product_context`、`get_mfgorder_progress`、`get_container_details` 等名称仅为后续设计候选，并未在本次新增。

证据文件：`docs/mcp-tool-audit-evidence.json`。其中区分了已读 Swagger 的端点和仅在服务目录中发现的关联分组。
