# 第3部分：设备资源

2026-11-09：设备主数据维护、维护要求激活/停用、资源设置及状态转换、资源吞吐、普通设备采集五项核心能力已完成开发和独立现场验收。新增13个MCP工具，同时接入本地Agent路由、确认摘要和外部MCP初始化说明。

## 入口

| 业务 | 工具 | 实际行为 |
| --- | --- | --- |
| 主数据 | `prepare_resource_change` | 新建、修改描述/备注、厂商型号/序列号、别名、连接名称、配方模式、清场级别及明确配置引用 |
| 维护要求启停 | `prepare_resource_activation` | 为一个设备激活或停用明确版本维护要求；支持明确日期、周期日期、吞吐类型，日期要求已现场验收 |
| 设置和可用性 | `prepare_resource_setup` | 原生ResourceSetup；明确Setup/产品版本、Run、可用性和状态/原因；省略时保留已有Setup/产品/Run |
| 状态转换 | 同上，`transition=true` | 原生ResourceSetupTransition；核对状态模型和目标状态码，不跳过MES规则 |
| 本次资源吞吐 | `prepare_resource_throughput` | 原生ResourceThruput；主副数量分别记录，明确各自单位，不换算，不代替容器报工或工单完工 |
| 普通设备采集 | `prepare_resource_collection` | 原生CollectResourceData，明确UserDataCollectionDef版本和点值，不需要容器；不推断质量结论 |
| 单次执行 | `execute_resource_change` | 重读目标及依赖、比对不可变摘要、记录尝试后只提交一次 |
| 核实 | `reconcile_resource_change` | 设置/吞吐/采集核对唯一标记、实际历史及明细；维护激活核对原生成功确认和维护关联回读 |
| 原计划和审计 | `get_resource_change_plan`、`review_resource_change` | 查询原响应/终态、人工最终证据审计及崩溃恢复；不重发原请求 |
| 状态与历史 | `get_resource_operational_state`、`get_resource_history` | 明确设备持久身份，读取ProductionStatus、维护关联及直接事务历史 |
| 吞吐明细 | `get_resource_throughput_history` | 每次主副数量、原单位ID、产品、设置、Run、事务ID、本地/GMT时间 |
| 采集明细 | `get_resource_collection_history` | 实际普通采集点名、值、类型、限值、定义/点ID及完整事务父链 |

所有prepare入口仅准备计划；核对请求与前后值，再使用同一 `plan_id / plan_digest` 执行并核实。计划有效期一小时，已使用计划不能重放。显示摘要不裁剪存储请求；完整依赖和原生回显保存在计划库，`get_resource_change_plan(include_details=true)` 可请求详细内容，超出输出上限时明确报错。

主数据引用字段为 `factory / resource_type / resource_family / maintenance_class / status_model / vendor`，须明确存在的对象名称；已有设备的状态模型迁移不在普通资料修改中替换。资料字段长度按现场持久契约校验，厂商型号和序列号最多30个UTF-16字符单元，Run最多30，设备名最多100。

## 两个不同的“激活”概念

`ResourceActivation` 激活的是设备的维护要求，不等于开机、Up或生产就绪。其 `maintenance_req_kind` 明确为 `date / recurring_date / throughput`；未新建维护要求，不覆盖维护类激活。日期要求启用和停用已现场验证，另外两类的到期/吞吐触发规则未验收。

设备可用性用资源设置事务，`availability=1` 为Up，`2`为Down。配置状态模型时，目标状态码及可用性必须一致，转换规则由MES继续验证。维护要求激活不等于已经完成维护任务；第3部分不包含第G08项维修、保养工单和完成维护流程。

## 现场验收结果

独立前缀：`CDX-R3-20261109`。本轮保留13个顶层测试对象，没有修改既有生产对象。

最终验收设备为 `CDX-R3-20261109-COMPLETE2`：

- 设备创建、资料修改通过请求字段及未修改配置回读。
- 绑定独立状态模型，以允许的初始Down进入，再完成Down→Up→Down；Setup、产品、Run在后续转换中保留。
- 给设备激活未来日期维护要求，再停用；最终无该维护关联。
- 记录主数量7、副数量3，分别使用EACH、UNIT；实际ResourceThruputHistory数量、单位及唯一事务链匹配。
- 提交Temperature=25、Ready=false、Note文本三点；DataPointHistory及三个实际点值均与请求匹配。
- 最终设备Down，累计原始计数为7和3，Down状态下新吞吐准备被阻断。
- 吞吐专用历史查询返回1条，普通采集专用历史查询返回3条，均完整。

原始本地证据：`tmp/resource-acceptance/journal.jsonl`，资源计划库为 `data/resource_plans.sqlite3`；采集定义维护使用已有 `data/collection_modeling_plans.sqlite3`。验收脚本为 [resource_acceptance.py](../scripts/resource_acceptance.py)，先只读生成唯一前缀清单，再执行该清单；请求前写日志、异常停止、不自动重试、不删除证据。

相关自动化回归224项通过，覆盖资源计划/历史/限值、原有资源读取、采集定义和分组读取、BOM兼容、能力目录、Agent确认摘要及流程读取。MCP进程内工具列举、schema和调用检查通过，当前源码共330个工具，其中新增资源工具13个。独立验收脚本的只读清单生成已验证。未将存在历史资料缺失的全仓测试宣称为全部通过。

## 本轮查明并修正的现场契约

1. Modeling/Resources的Availability回读为空，写入该字段也不设置实际ProductionStatus可用性。因此主数据入口禁止写运行状态，实际状态由专用持久读取和原生设置事务核实。
2. ResourceActivation不产生HistoryMainline记录，维护关系保存在AssignedMaintReq与MaintenanceStatus。因此启停核实必须同时有原HTTP明确成功确认和准确的维护关联回读；丢失响应保持unknown，不凭相同状态宣告成功。
3. 日期维护要求使用具体DateReqs类型；通用MaintenanceReqs基类维护请求在本环境被拒绝。未修改账号权限，未绕过授权。
4. 嵌套 `Groups($expand=Points)` 返回的组ID是临时Changes身份，组标量为空。现分别读取浅层Groups与嵌套Points，按唯一名称合并，保留真实组ID、最少样本数与顺序。
5. 分组采集点的ColumnPosition、RowPosition等继承字段在现场不可写，尽管Swagger将其列为必填。分组点维护不再自动发送这些普通点布局字段，也不静默接受显式不支持字段。

## 保留的限制

**分组资源采集执行关闭。** 本环境CollectResourceData能完整回显DPCollection分组样本，并返回提交成功，但实际没有生成对应样本历史。工具在准备阶段阻断 `collection_kind="point"`，不转换成普通点、不重发原分组请求，也不将预览/HTTP 200当作数据落账。普通User采集已完成实际落账验收。

探索阶段的 `CDX-R3-20261109-FINAL` 保留一笔未核实分组请求；该设备和相关定义继续占用。最初Availability写入探测设备 `...-DEVICE`、过长序列号失败计划 `...-COMPLETE`，以及最初分组布局字段失败定义也保留原记录，未伪造成功或人工解除占用；正式通过验收的是上述COMPLETE2设备。

运行事务限独立设备，不执行父子设备级联、载具、零件、高容量追溯或工具更换。设备删除、状态模型整体迁移、维护任务执行、OEE/排程不在本次范围。不提交跳过签名、培训、维护或状态模型的参数。

ProductionStatus累计计数在本环境未同步保存单位ID或LastThruputDate，专用状态查询保留其空值并提示按实际吞吐历史逐条核对单位，不能据空时间判定设备没报过吞吐，也不能混合单位计算总量。

超时、响应丢失、历史缺失或回读不匹配时保持unknown；同设备及相关定义禁止通过新计划绕过占用。旧计划永不重放。人工resolve必须有最终证据，恢复遗留in_flight只转unknown并继续占用。

客户端执行前重读属于变更检查，不能替代MES对其他外部客户端并发修改的事务控制。维护类/自动状态模型或业务规则仍由MES负责最终验证。

部署时需重启当前8030服务并刷新WorkBuddy MCP清单；本次未终止用户在VS Code启动的进程。
