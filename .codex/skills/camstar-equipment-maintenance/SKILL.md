---
name: camstar-equipment-maintenance
description: 在 Camstar / Opcenter 中查询设备维护状态，启停预防保养要求，记录和追溯设备点检，处理故障停机、维修交接与复机登记。用于设备级业务，区分资源事务与尚未接入的保养、维修工单。
---

# 设备保养、点检与维修

先识别用户要查询、准备计划，还是登记已发生的业务。设备名称有歧义时，用 `list_resources` 定位实例，让用户选定设备。配置资料用 `get_resource_details`，运行可用性、当前状态和维护关联用 `get_resource_operational_state`。按设备名称及已确认的 `resource_instance_id` 准备事务。运行事务仅支持独立设备；遇到冻结、锁定、父子设备或状态读取不完整时，报告具体阻断原因。

## 预防保养

- **查保养要求**：读取设备的维护关联；需要建模侧 PMStatus 时，用 `get_resource_details(include_maintenance=true)`。区分查询不完整、未找到关联和已关联。回答“何时到期”时，指出返回的日期字段及含义；激活日期本身不足以计算下次到期日。
- **启用或停用已有要求**：用 `prepare_resource_activation`，提供设备、要求名称、明确版本、`activated` 和 `maintenance_req_kind`。指定日期用 `date`，周期日期用 `recurring_date`，按吞吐量用 `throughput`。日期要求启停已有现场验收；周期和吞吐要求的自动触发效果仍待现场核实。只有明确要求修改设备维护类引用时才用 `prepare_resource_change` 的 `maintenance_class`。
- **核对按量保养的依据**：用 `get_resource_throughput_history` 逐笔核对数量、原单位及时间；主副计数分别解释。累计计数缺少单位时，依据明细回答，保留不同单位。
- **用户要新建保养任务或登记完工**：现有入口只能关联已有要求。整理设备、要求及版本、计划或实际时间、作业内容和已有凭证，标明待在现场保养流程登记的事项。停用维护要求会移除关联，不能用来登记完工。

## 设备点检

1. 明确设备、点检定义名称和版本。如需查点表，用 `get_data_collection_details(name=…, revision=…, kind="user")` 查看点名、类型和约束，按需翻页。记录点检需要 Active 的普通设备采集定义和实际观测值。
2. 用 `prepare_resource_collection`，传 `definition_name`、`definition_revision`、`collection_kind="user"` 和按原点名组织的 `values`。例如定义中确有这两个点且现场提供了读数时，传 `{"Temperature": 25, "Ready": false}`。保留零值和布尔假值。缺少必填读数时只询问缺项。当前分组样本采集（`point`）因缺少持久历史而关闭，保留原样本结构并说明限制。
3. 查某次点检时，用 `get_resource_collection_history` 按设备和可选 `txn_id` 读取点值、时间及限值。查询最新结果时核对事务时间，避免把不同轮次的点拼成一次点检。按已有点检标准解释异常；标准缺失时只报告观测事实。发现异常可整理报修资料，停机登记须有明确业务指令。

## 故障停机与维修交接

- **报修或查维修进展**：读取设备当前状态，用 `get_resource_history` 核对停复机事务，需要异常读数时补查该次点检明细。交接材料写明设备、故障时间和现象、当前状态、关联事务号，以及现场提供的处置和复测结果。对未提供的内容标明待补。当前项目未接入报修开单、派工、维修完工或关闭工单；相应请求交付可录入现场系统的草稿，并明确实际登记进度。
- **登记停机**：用户明确要求将设备登记为不可用时，用 `prepare_resource_setup(availability=2, …)`。按设备状态模型转换时，提供 `transition=true` 及模型允许的目标 `status_code`，有明确原因码时传 `status_reason`。状态码的可用性必须与目标一致，保留无关的 Setup、产品及 Run。
- **登记复机**：用户明确要求恢复可用时，先重读维修后的实际状态，再按当前模型准备 `availability=1` 的新计划。回复中分别说明设备可用性、复测证据和工单状态。仅收到“已修好”或查询请求时，按提供的事实回答，勿自行提交复机。

## 提交与核实

所有 `prepare_resource_*` 工具只生成计划。展示设备、动作、关键前后值或点值及计划标识；用户确认该计划后，用同一 `plan_id` 和 `plan_digest` 调用一次 `execute_resource_change`。若执行前检查明确返回 `submitted=false`，按原因重新读取，变化后的新计划重新确认。

已尝试提交且状态为 `unknown` 时，用原计划调用 `reconcile_resource_change`。维护要求启停核对原生成功响应和维护关联回读；设置和点检核对实际事务及对应明细。仅在 `verified_success=true` 后报告已核实。响应丢失或证据不匹配时，保留计划号和未知状态，用 `get_resource_change_plan` 查询原记录，不重发或另建计划绕过占用。

现场验收范围和已知限制见仓库的 [设备资源维护说明](../../../docs/resource-maintenance.md)。
