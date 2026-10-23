# 核心周边能力开发任务与进度

创建日期：2026-10-22（Asia/Shanghai）。状态：T00 完成；T01–T04 核心只读实现已落地，完整现场及聊天端验收继续跟踪。

本文是后续持续开发的入口。每次开始先核对代码与当前服务契约，再完成一个可验收的任务；结束时更新本文进度与证据。候选工具名是设计建议，不表示已注册或可调用。

## 1. 目标与已有基础

围绕产品、工艺、工单、容器补齐设备资源、BOM 用料、数据采集等能力，最终支持：

> 帮我检查工单 MO001 开工前的配置是否齐全，列出工艺、设备资源、物料和采集要求，以及需要补充的信息。

已有实现应复用：

- 产品工艺链、路线结构：`tools/process_context.py`、`tools/workflow_steps.py`。
- 建模完整性与版本比较：`tools/modeling_workspace.py`。
- 产品、工序、工单关联详情：`tools/modeling_details.py`。
- 工单进度：`tools/order_progress.py`。
- 容器详情、历史、追溯：`tools/container_inquiry.py`、`tools/container_trace.py`、`tools/container_genealogy.py`。
- 冻结、解冻、原因码、数量变更：`tools/production_basics.py`。

不要把旧审查文档里的“未接入”直接作为当前缺口。以代码注册清单和实际调用能力为准。完整工单下发、整单报工、自动排产、OEE、ERP 同步不在本计划首期范围内。

## 2. 契约基线与证据边界

2026-10-22 已实时 GET 以下分组的 `/swagger/{分组}/swagger.json`，均返回 HTTP 200，并读取了关键 schema：

| 服务 | 分组 | 本次已确认内容 |
|---|---|---|
| Modeling | Resources、ResourceGroups | 资源及资源组；资源含 availability、currentSetup、pmStatus、父子资源等字段，资源组含 entries、groups、resolvedEntries |
| Modeling | BOMs | 带版本的 BOM，包含 materialList |
| Modeling | DataCollectionDefs | 带版本的采集定义，包含 parametricDataDef、parametricDataDefType 等引用 |
| Modeling | MfgOrderTaskLists | 任务列表、执行模式、任务及前置任务列表引用 |
| Modeling | DocumentSets | documentEntries 文档条目 |
| Shopfloor | ComponentIssue | container、serviceDetails 为 schema 必填字段 |
| Shopfloor | CollectData | container 为 schema 必填字段；有采集定义与 parametricData 字段 |
| Shopfloor | ExecuteMfgOrdertask | mfgOrderProcedure、mfgOrderTask 为 schema 必填字段 |
| Shopfloor | Split、Combine | 拆批、合批；含拆分目标/合并来源、数量相关明细 |
| Shopfloor | Associate、Disassociate | 容器父子关联与解关联 |

2026-10-23 已重新读取并归档 15 份脱敏契约，见 `Swagger/extensions/` 和 [T00 基线](core-surrounding-baseline.md)。Swagger 可读不代表实例查询、字段展开、权限或业务事务已经验收；schema 的 required 也不代表全部现场必填规则。

## 3. 开发顺序与总进度

按 T00 → T01 → T02 → T03 → T04 → T05 → T06 → T07 → T08 → T09 执行。每阶段先完成最小可用闭环，再决定是否扩展维护操作。

| 完成 | 编号 | 任务 | 前置 | 当前状态 |
|---|---|---|---|---|
| [x] | T00 | 契约归档与现状核对 | 无 | 完成：15 份实时契约、37 次只读检查，样本限制已记录 |
| [ ] | T01 | 设备资源与工序资源查询 | T00 | 实现完成、46 项回归通过；非空现场/聊天端验收待补 |
| [ ] | T02 | BOM 用料与版本差异查询 | T00 | 实现完成、57 项相关测试通过；非空现场/聊天端验收待补 |
| [ ] | T03 | 数据采集定义与工序要求查询 | T00 | 核心实现完成，69 项相关测试通过；枚举/动态规则及完整现场关联待核实 |
| [ ] | T04 | 开工准备配置检查 | T01–T03 | 核心汇总实现完成；78项测试、逐采集定义汇总实现；完整现场验收待补 |
| [ ] | T05 | 投料与消耗事务 | T02 | 进行中：意图/需求/历史只读工具完成；需求与历史样本为0，执行链待验 |
| [ ] | T06 | 数据采集提交 | T03 | 基础开发：校验、预览、单次提交、候选事务逐项核对已实现；现场关联/恢复待集中补测 |
| [ ] | T07 | 拆批、合批、装箱与拆箱 | T00、现有容器查询 | 进行中：四类意图预览、三类不执行服务回显预览；37项测试通过，执行契约与闭环待补 |
| [ ] | T08 | 工单过程任务查询与执行 | T00、现有工单查询 | 待开始 |
| [ ] | T09 | 作业指导书与文档关联 | T00、现有 Spec 查询 | 待开始 |

首期里程碑：T00–T04 完成，形成可用的只读配置检查。第二期：T05–T06 完成，接入投料和采集执行。第三期：T07–T09 按现场需求推进。

## 4. 分阶段任务

### T00：建立可复用契约基线

- [x] 核对现有工具、目录策略、按需加载和相关测试，记录可复用函数及真实缺口。详见 [T00 基线](core-surrounding-baseline.md)。
- [x] 从配置中的 Modeling / Shopfloor 服务重新读取上述 Swagger；必要时按其引用继续核对 Query、采集点等分组。
- [x] 建议将脱敏契约保存到 `Swagger/extensions/`，记录读取日期、相对地址、方法、schema、版本/文件摘要与响应状态；不保存凭据或鉴权头。
- [x] 核对 key、版本/ROR、引用结构、`$select`/`$expand`、分页、响应与错误格式；不能仅凭字段名猜测行为。
- [x] 为后续只读验证选择现有样本；列明无样本、无权限或未配置的领域，不为查询测试创建业务数据。

交付：契约文件、`docs/core-surrounding-contract-evidence.json`（建议文件名）及样本/限制清单。

验收：每项候选能力可追溯到真实接口与 schema；仅发现接口、模拟测试通过、真实查询通过三种状态分开记录。

### T01：设备资源与工序资源查询

建议模块：`tools/resources.py`。候选工具：`list_resources`、`get_resource_details`、`get_spec_resource_context`。

- [x] 支持资源列表、分页、详情及明确的对象定位。
- [x] 解析 Spec 资源组引用，读取成员；核对嵌套资源组、重复成员和循环引用。
- [x] 展示已验证可读取的资源层级、当前 Setup、可用性及维护状态，保留来源与原始状态值。
- [x] 缺失资源组、空成员、未知状态应明确标记；配置的候选资源不能直接等同于允许开工的设备。

验收问题：“工序 A 配置了哪些资源？”“设备 B 属于哪个层级、当前配置是什么？”覆盖正常、空组、缺失引用、分页和读取失败。

实施与证据：[T01 工具说明](resource-query-tools.md)。以上勾选表示实现完成；非空现场与聊天端验收待补，阶段总验收保持未勾选。

### T02：BOM 用料与版本差异查询

建议模块：`tools/boms.py`。候选工具：`get_bom_details`、`get_product_material_requirements`、`compare_bom_versions`。

- [x] 定位产品 BOM 与指定版本，明确显式版本和 ROR 的选择规则。
- [x] 完整读取 materialList，核对物料、数量、单位、工序关联等真实字段及导航。
- [x] 区分产品标准 BOM 和工单实际物料清单；冲突或覆盖规则未核实时并列展示。
- [x] 比较新增、删除与修改的明细；按已验证稳定标识匹配，不能只按数组顺序比较。
- [x] 只在数量基准、单位和损耗规则明确时计算需求量；未接库存数据时不输出“库存充足”。

验收问题：“产品 P 的 A 版需要什么物料？”“A 版和 B 版的用料差异是什么？”覆盖版本歧义、空清单、重复物料、单位差异和分页。

实施与证据：[T02 工具说明](bom-query-tools.md)。以上勾选表示代码及模拟场景完成；非空 BOM、实际工单关联及聊天端验收仍待补，阶段总验收保持未勾选。

### T03：数据采集定义与工序要求查询

建议模块：`tools/data_collection.py`。候选工具：`get_data_collection_details`、`get_container_collection_requirements`。

- [x] 沿 DataCollectionDefs 的真实引用继续核对采集点定义；确认是否需要 DataPointCollectionDefs / UserDataCollectionDefs 等契约。
- [x] 核对容器当前工序及其采集定义的关联/选值方式，不自行拼接关系。
- [x] 在契约和实例支持时返回采集点、类型、单位、必填、允许值及上下限；未知约束显式标记。
- [x] 区分静态定义与当前事务动态要求；不把数值范围检查等同于整个批次质量判定。

验收问题：“容器 C 当前需要采集哪些数据？”覆盖数值、文本、枚举、无定义、版本歧义及动态约束无法读取。

实施与证据：[T03 工具说明](data-collection-tools.md)。已实现项以静态查询和未知项标记为边界；枚举选值、动态约束、分组点非空、完整容器关联及聊天端验收仍待补，阶段总验收保持未勾选。

### T04：开工准备配置检查

建议模块：`tools/production_readiness.py`。候选工具：`check_mfgorder_readiness`。

- [x] 聚合工单、产品工艺链、工序资源、BOM 与采集要求；复用现有领域函数，不要求模型手动拼接大量请求。
- [x] 各检查项返回对象、来源、结论、说明和建议动作；结论使用“通过 / 缺失 / 未知 / 不适用”。
- [x] 独立只读查询可受控并发；限制递归深度、分页与总请求数，并展示未完成范围。
- [x] 部分接口失败仍返回其他证据，整体标记检查不完整，不误报通过。
- [x] 总结用“已检查范围内配置齐全”等准确表述；不据此自动 Start，也不声称覆盖库存、资质、设备实时状态等尚未检查的条件。

验收问题：“检查工单 MO001 的开工配置。”至少验证配置完整、缺失关联、部分服务失败三类情况；首次查询与多次重复查询结果应一致且无业务写入。

实施与证据：[T04 工具说明](production-readiness-tools.md)。工单/工艺/资源/物料已聚合，采集已逐定义汇总点约束，未知类型与动态要求保留未知；总验收暂不勾选。

### T05：投料与消耗

候选工具：`preview_component_issue`、`issue_components`；命名与实现可按实际契约调整。

- [ ] 核对 ComponentIssue 明细类型、追踪方式、物料批号/容器引用、数量、单位及工序条件。
- [ ] 生成明确的投料预览，列出目标容器、物料来源、数量与未满足条件；执行前重新读取关键状态。
- [ ] 将执行工具登记为写入，复用现有确认机制；明确聊天入口与直接 MCP 客户端的执行边界。
- [ ] 成功后按已核实的查询/历史接口回读；超时或断连时标记“结果待核实”，先查询再决定是否重试。
- [ ] 无产品幂等契约时不得声称可安全重放；确认结果必须与执行内容一致。

验收：模拟正常、错误物料、数量越界及响应丢失；在明确的测试样本上验证消耗数量、来源关联与历史记录。缺少测试样本时保留“现场验收待完成”。

当前交付：[T05 契约与下一步](component-issue-development.md)。只读意图预览已实现，executable=false；尚未实现投料执行，不勾选执行相关任务。

### T06：数据采集提交

候选工具：`validate_collection_values`、`collect_container_data`。

当前交付：`validate_collection_values` 已注册到 collection 能力组；支持 UserDataCollectionDef 静态必填、类型和常量限值检查。现场定义只读校验通过，未提交数据。详见 [T06 开发说明](collection-submission-development.md)。后续条目保持未完成，因为动态规则与实际提交契约尚未确认。

- [ ] 根据 T03 的真实定义构造 CollectData 的 parametricData，核对其具体类型和序列化格式。
- [ ] 提交前检查必填、类型和已知限制；单位换算仅在明确支持时执行。
- [ ] 明确越限值是禁止提交、允许记录还是触发异常，遵循现场业务规则，不能统一拦截或擅自判合格。
- [ ] 执行前重新核对容器工序与定义版本，展示采集值并走现有确认流程。
- [ ] 回读采集记录；对重复提交、超时和部分失败给出可核实状态。

验收：覆盖合法值、缺项、错误类型、越限、工序已变更和响应丢失；测试样本实际提交结果可回读。

### T07：拆合批与父子关联

当前交付：[T07 开发记录](container-transfer-development.md)。四类独立只读预览已实现，未封装实际执行。

- [ ] 分别封装 Split / Combine 与 Associate / Disassociate，不混为一种操作。
- [ ] 核对容器身份、层级、产品、数量、目标命名、状态和服务规定的兼容条件。
- [ ] 预览操作前后数量或父子关系；涉及 closeWhenEmpty、reduceQty、disassociateAll 等字段时明确其效果。
- [ ] 执行后结合已有追溯查询核对数量与关系；验证一次失败或超时后的核实路径。

验收：拆批/合批按真实数量规则核对，关联/解关联按父子关系核对；部分失败不得报告全成功，不提供未经验证的自动回滚。

### T08：工单过程任务

- [ ] 查询工单过程、任务列表、执行模式、前置关系与已执行记录，复用现有 get_mfgorder_execution。
- [ ] 验证 ExecuteMfgOrdertask 的任务引用、状态、pass 和适用工单上下文。
- [ ] 先提供待执行任务查询，再增加指定任务结果提交与回读；后续可将待办摘要加入 T04。

验收：能解释任务来源与执行状态；覆盖前置未完成、任务不存在、重复执行。明确这是过程任务，不宣称已完成产量报工或整单完工。

### T09：作业指导书与文档关联

- [ ] 解析 Spec → DocumentSet → documentEntries，展示文档标识、版本及来源。
- [ ] 查询缺失文档关联；只有业务要求该工序必须有文档时才判缺失为问题。
- [ ] 核对文件下载/预览端点与访问权限；没有可验证地址时只提供元数据。
- [ ] 可将文档配置情况加入 T04，不将文档内容作为自动执行指令。

验收：正常、空文档集、失效引用与无访问权限均能正确展示；链接必须来自真实响应。

## 5. 每个任务的统一实施流程

1. 阅读本文当前进度，检查工作区已有修改及相关实现，避免覆盖其他工作。
2. 读取本阶段契约和必要关联 schema，记录真实样本及待确认语义。
3. 明确最小输入/输出：名称与 ID、版本、分页、来源、完整性和错误状态。
4. 实现领域函数及 MCP 工具，复用 `core/http_client.py`、现有引用解析和响应处理；大结果不能静默截断关键明细。
5. 在 `tools/__init__.py` 注册并核对 `get_tool_func`；在 `core/tool_catalog.py` 声明 effect、分组与确认策略；同步能力发现和按需加载，避免仅注册却无法被 Agent 使用。
6. 对关键业务边界做单元测试、工具注册/策略回归及只读集成验证；写入能力另记录测试环境事务验收。
7. 从聊天端验证自然语言请求、澄清歧义、错误说明和结果完整性；工具客户端另核对清单与参数。
8. 保存验证证据，更新本文件的状态、完成清单、遗留项和下一步。部署 Python 工具变更后重启服务并刷新客户端工具清单。

已有基础回归入口（新增领域另加对应测试）：

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_capability_discovery.py tests/test_agent_safety.py -q
```

阶段完成必须同时满足：契约可追溯、工具可发现/执行、关键测试通过、结果边界明确、文档已更新。只有模拟测试通过时，不能将现场验收标为完成。

## 6. 持续开发记录

当前下一步：**续接 T05：核实 LoadRequirements、真实需求行及消耗回读，再实现执行链**。T01–T04 完整现场和聊天端验收继续保留，未知配置不得视为通过。

| 日期 | 任务 | 本次结果 | 验证证据 | 未完成/阻塞 | 下一步 |
|---|---|---|---|---|---|
| 2026-10-22 | 规划 | 已保存任务顺序、范围与验收标准；未新增业务工具 | 本文；前次会话的 Swagger GET 核对 | 完整契约响应待归档 | T00 |
| 2026-10-23 | T00 部分完成 | 132 工具策略/分发核对；10 项测试通过；只读审计脚本与两份缓存契约已保存 | [基线](core-surrounding-baseline.md)、[证据](core-surrounding-contract-evidence.json) | 15 个分组连接超时；新分组 key/ROR/分页/展开和样本待核实 | 续接 T00，完成后进入 T01 |
| 2026-10-23 | T00 完成 | 15 份实时契约；37 次只读检查（35 成功、2 项明确限制/错误探测）；10 项回归通过 | [实时验证](core-surrounding-read-validation.json)、[基线](core-surrounding-baseline.md) | 六类对象无可见样本；ESigHistoryDetails 展开被拒绝，后续按领域跟踪 | T01 |
| 2026-10-23 | T01 实现完成 | 3 个只读工具、resource 分组；46 项测试通过；空资源列表/缺失对象/Spec 引用不足现场读取通过 | [工具说明](resource-query-tools.md)、[现场证据](resource-query-validation.json) | 非空资源组、PMStatus、聊天端完整回合待验收；未重启服务 | T02，并持续跟踪 T01 验收 |
| 2026-10-23 | T02 实现完成 | 3 个只读工具、material 分组；57 项相关测试通过；现场空 BOM 与产品缺少可解析 BOM 引用处理通过 | [工具说明](bom-query-tools.md)、[现场证据](bom-query-validation.json) | 非空 BOM、真实版本差异、工单关联及聊天端待验收；未重启服务 | T03，并保留 T01/T02 验收项 |
| 2026-10-23 | T03 核心实现完成 | 2 个只读工具、collection 分组；69 项相关测试；用户采集定义非空查询通过，容器当前 Spec 缺失正确报告未知 | [工具说明](data-collection-tools.md)、[现场证据](data-collection-validation.json) | 分组点非空、完整容器关联、枚举/动态规则与聊天端待验收 | T04，并保留前序限制 |
| 2026-10-23 | T04 核心汇总完成 | 新增只读开工配置检查；75项相关测试；真实工单2项通过/8项未知，4工序未检查 | [说明](production-readiness-tools.md)、[现场证据](production-readiness-validation.json) | 逐采集点约束、完整现场与聊天端待补；未重启服务 | 细化T04采集，再推进T05 |
| 2026-10-23 | T04 采集汇总补齐 | 逐定义点约束、类型证据、去重和有界读取；78项测试，真实工单只读复核 | [采集汇总证据](production-readiness-collection-validation.json) | 非空完整映射、动态规则和聊天端待验收 | T05 |
| 2026-10-23 | T05 部分完成 | 契约区分累计量与本次量；只读意图预览接入，34项相关测试及现场身份读取通过 | [说明](component-issue-development.md)、[证据](component-issue-preview-validation.json) | LoadRequirements、真实BOM行、来源校验、成功回读及执行链未完成 | 继续T05 |
| 2026-10-23 | T05 需求匹配查询 | 新增ComponentIssueInquiry固定只读封装并接入预览；37项测试通过 | [需求证据](component-requirement-validation.json) | 当前样本无匹配需求；非空行、消耗回读和执行链仍待核实 | 继续T05真实需求及执行契约验证 |
| 2026-10-23 | T05 历史回读 | 固定只读实际投料历史查询；40项测试；现场三类需求/历史表计数为0 | [历史证据](component-issue-history-contract.json) | 缺少配置需求的测试容器及来源物料，执行链不能完成验收 | 提供测试样本后续接T05，或独立推进T06 |

| 2026-10-23 | T06 历史查询 | 采集历史只读工具接入能力列表，51 项相关测试通过，现场空页查询成功 | [证据](collection-history-contract.json) | 历史明细计数为 0，非空关联、提交格式和事务验收待验证 | 继续核实 UserDataCollection 的动态提交结构与测试样本 |

| 2026-10-23 | T06 提交基础流程 | 元数据补齐子类型，execute=false 回显通过；不可变计划、确认、复核与防重提交实现 | [开发记录](collection-submission-development.md) | 用户要求现场测试集中延期；成功历史关联/未知状态恢复待补 | 继续独立开发；T00–T09 后按集中清单补测，不将 T06 标为端到端完成 |

| 2026-10-23 | T06 候选事务核对 | 只读比较计划与历史，保留未知结果锁；能力目录已更新 | [说明](collection-submission-development.md) | 候选事务尚不能自动证明请求来源，不解除未知锁 | 按用户约定继续T07独立开发，后续集中补测T06关联与恢复 |

| 2026-10-23 | T07 预览层 | 四类独立意图预览、能力目录与路由接入；27项测试与现场只读检查通过 | [说明](container-transfer-development.md) | Split数量输入路径、兼容规则与执行回读待补 | 继续T07服务契约与可执行计划 |

| 2026-10-23 | T07 服务预览 | 合批/装箱/拆箱支持可选execute=false回显检查，37项相关测试通过 | [说明](container-transfer-development.md) | Split数量入口、深层关系/兼容性、现场回显与提交仍待补 | 继续T07完整校验与提交计划 |

每次追加一行，并更新第 3 节状态。不要将“已实现”“模拟验证通过”“真实查询通过”“现场事务验收通过”合并成同一个完成标记。若任务只完成部分，在对应阶段逐项勾选并写清剩余工作。

## 7. 下次开发可直接使用的提示词

> 请阅读 docs/core-surrounding-capabilities-roadmap.md，核对当前代码和进度，从第一个未完成任务开始开发。本次先完成一个可验收阶段，复用现有能力。保存契约与测试证据，结束时更新该文档的任务状态、遗留问题和下一步。若缺少现场样本，继续完成不依赖样本的实现与验证，并明确区分实现完成和现场验收状态。

相关已有说明：[生产基础能力](production-basics-tools.md)、[工具加载与详情](tool-loading-and-details.md)、[产品工艺上下文](process-context-enhancement.md)、[工单进度与导入](import-plans-and-order-progress.md)。

## 集中补测约定（2026-10-23）

用户要求：先完成不依赖现场写入的开发，所有 T 阶段开发后统一补之前遗漏的测试。现场未验收阶段不勾选完成。持续维护 [集中验收清单](core-surrounding-deferred-tests.md)，结束时补齐数据准备、步骤、预期结果与证据。
