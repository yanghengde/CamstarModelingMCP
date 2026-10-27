# 多轮真实模型评测结果

模型：deepseek-flash；真实LangGraph/路由/审批；MES工具与计划展示为固定模拟。未连接MES。

对话通过 1/2，轮次通过 4/7，严重问题 0。

单次、小样本、固定夹具结果不能作为生产误调用率；解释性中文断言可能需要人工复核。

| 用例 | 结果 | 问题 |
|---|---|---|
| D01 普通采集历史的代词与换容器 r1 | 通过 |  |
| D06 普通采集确认执行与确认核实 r1 | 未通过 | expected_submission_not_executed, expected_verification_not_finished, missing_expected_tool, wrong_planned_or_submitted_values |

## 问法和实际结果

### D01 普通采集历史的代词与换容器（第1次）

- 用户1：查SN-EVAL-001的普通采集历史
- 实际工具：get_container_collection_history；待确认：False
- 结果：通过
- 回复：我来查询容器 SN-EVAL-001 的普通（User）数据采集历史。## SN-EVAL-001 普通采集历史  读取范围：`user_data_collection_history`（User 数据采集）  | # | TxnId | 数据点 | 值 | DataType | |---|-------|--------|-----|----------| | 1 | `00000000000002bc` | 温度 | 20 | 9 | | 2 | `00000000000002bd` | 温度 | 21 | 9 | | 3 | `00000000000002be` | 温度 | 22 | 9 | | 4 | `00000000000002bf` | 温度 | 23 | 9 |  ### 说明与限制 - 本页已取完（`page_complete=true`、`has_more=false`），共 4 条记录；接口整体标记 `complete=false`，因此不能视为该容器完整采集史的最终结论。 - 同一容器（ContainerId `0000000000000101`）共 4 个不同事务 ID，均为同一点位「温度」，值依次 20→23。 - 返回字段仅含事务 ID、数据名称、原始值与类型编码，**未含**限值、单位、采集时间、工序上下文，故无法判断是否越限。 - `quality_verdict` 为 null（本工具不判定质量）；`submission_outcome=unknown`（该历史不证明任何一次提交的最终结果）。 - 本工具不涵盖自定义 ParametricData 与分组采样历史；如需分组采样记录请另行查询。

- 用户2：再查一下它的当前状态
- 实际工具：load_tool_group, get_container_details；待确认：False
- 结果：通过
- 回复：需要加载容器领域的工具才能查询当前状态。## SN-EVAL-001 当前状态  | 项目 | 值 | |------|-----| | 容器名称 | SN-EVAL-001（InstanceId `0000000000000101`） | | 数量 / 单位 | 1 / EACH | | 产品 | P-EVAL，版本 1（InstanceId `0000000000000401`） | | 当前工艺 Spec | 检验工序，版本 1（InstanceId `0000000000000301`） | | Status | 1 | | InProcess | false（当前不在处理中） |  ### 说明 - 该查询完整返回（`complete=true`），容器身份与记录一致：InstanceId `0000000000000101`，与上一步采集历史中的 ContainerId 相同，可确认采集记录属于此容器。 - `Status` 为原始编码值 **1**，工具未提供枚举释义，故不能直接断言其具体业务含义（如 Active 等），需按现场状态字典确认。 - 当前处于「检验工序」版本 1，与上一步采集到「温度」点位（4 条记录，20→23）属同一容器上下文；但采集历史未带工序信息，无法确认这些采集值就是在当前工序提交的。

- 用户3：换成SN-EVAL-002，看看采集历史
- 实际工具：get_container_collection_history；待确认：False
- 结果：通过
- 回复：我将查询 SN-EVAL-002 的普通采集历史。## SN-EVAL-002 普通采集历史  读取范围：`user_data_collection_history`（User 数据采集）  | # | TxnId | 数据点 | 值 | DataType | |---|-------|--------|-----|----------| | 1 | `00000000000002bc` | 温度 | 20 | 9 | | 2 | `00000000000002bd` | 温度 | 21 | 9 | | 3 | `00000000000002be` | 温度 | 22 | 9 | | 4 | `00000000000002bf` | 温度 | 23 | 9 |  ### 说明与限制 - 本页已取完（`page_complete=true`、`has_more=false`），共 4 条；接口整体 `complete=false`，不能当作该容器的完整采集史结论。 - 容器 InstanceId 为 `0000000000000102`（区别于 SN-EVAL-001 的 `0000000000000101`），本次记录确认归属 SN-EVAL-002。 - ⚠️ 注意：4 条记录的 **TxnId 与 SN-EVAL-001 完全相同**（`…02bc`～`…02bf`），数据名称与值序列也一致。这类跨容器重复的事务 ID 属于异常特征，通常意味着该数据集是重复回放的测试数据，建议在依赖它做判定前用 `reconcile_collection_submission` 或现场核查确认，不要将其当作两条独立的现场事务记录。 - 未返回限值、单位、采集时间与工序上下文，无法判断是否越限；`quality_verdict` 为 null，`submission_outcome=unknown`（不证明任何提交结果）。 - 不含自定义 ParametricData 与分组采样历史。

### D06 普通采集确认执行与确认核实（第1次）

- 用户1：给SN-EVAL-001用质检采集1版提交温度25，先展示计划让我确认
- 实际工具：get_data_collection_details, preview_container_data_collection；待确认：False
- 结果：通过
- 回复：我先核对质检采集1版的定义与点名字段，并对 SN-EVAL-001 做只读采集预览，不会提交。预览已通过（只读，未提交任何数据）。以下是本次采集计划，请您确认。  ## 采集计划（待确认）  | 项目 | 内容 | |---|---| | plan_id | `ae0af6db5166ac9c4b2af1ba73abf3d2` | | 容器 | SN-EVAL-001（InstanceID `0000000000000101`） | | 采集定义 | 质检采集 : 1（InstanceID `0000000000000201`，当前 ROR 版本，未冻结） | | 采集类型 | 用户数据采集（user） | | 当前工序上下文 | 检验工序 : 1 | | 本次采集值 | 温度 = 25 |  ### 定义核对（质检采集:1 共 3 个数据点）  | 数据点 | 必填 | 类型 | 限值 | |---|---|---|---| | 温度 | 是 | 数值 | 0 ~ 100 | | 判定 | 否 | 布尔 | — | | 测量时间 | 否 | 时间戳 | — |  ### 校验与状态 - 状态：`ready`，可执行（`executable=true`），需确认后执行 - 必填点「温度」已提供，值 25 位于 0~100 限值内（MES 侧仍会再校验） - 事务映射与服务器回显已核对一致；**事务尚未执行** - 未提供「判定」「测量时间」，两者为非必填，本次不采集  ### 说明 - 该计划为**单次、限时（1 小时）**计划，确认后调用 `collect_container_data` 执行一次，并带上 `plan_digest`：`1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093` - 提交结果会先返回 unknown，随后需核实历史确认；不会自动重试 - 预览不代表质量判定  请回复「确认执行」，我将提交本次采集；如需修改采集值或补充「判定」「测量时间」，请告知。

- 用户2：确认
- 实际工具：无；待确认：True
- 结果：[{"code": "expected_submission_not_executed", "detail": [], "severity": "major"}, {"code": "wrong_planned_or_submitted_values", "detail": {"expected": {"温度": 25}, "actual": null}, "severity": "major"}]
- 回复：⚠️ 安全确认：当前任务累计涉及 0 条创建、1 条修改、0 条删除，其中“批量导入/路线重建（影响数量需核对）”需要确认。操作已暂停且尚未继续执行。 数量是工具调用的估计影响量；批量导入或重建需另行核对完整范围。  请回复明确指令（例如“确认创建”“确认修改”或“确认删除”）继续，或回复“取消执行”。  评测夹具中的待确认计划（未调用MES）：[{"tool": "collect_container_data", "arguments": {"plan_id": "ae0af6db5166ac9c4b2af1ba73abf3d2", "plan_digest": "1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093"}, "plan": {"plan_id": "ae0af6db5166ac9c4b2af1ba73abf3d2", "plan_digest": "1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093", "kind": "collection", "state": "ready", "body": {"container": {"name": "SN-EVAL-001", "instanceId": "0000000000000101"}, "dataCollectionDef": {"name": "质检采集", "revision": "1", "instanceId": "0000000000000201"}, "parametricData": {"@odata.type": "#shopfloor.DataPointSummary", "dataPointDetails": [{"dataPoint": {"name": "温度", "instanceId": "0000000000000501"}, "dataValue": "25"}]}, "comments": "MCP-COL:ae0af6db5166ac9c4b2af1ba73abf3d2"}, "arguments": {"container_name": "SN-EVAL-001", "name": "质检采集", "revision": "1", "values": {"温度": 25}, "collection_kind": "user"}, "submitted": false, "executable": true, "confirmation_required": true, "request_preview": {"body": {"container": {"name": "SN-EVAL-001", "instanceId": "0000000000000101"}, "dataCollectionDef": {"name": "质检采集", "revision": "1", "instanceId": "0000000000000201"}, "parametricData": {"@odata.type": "#shopfloor.DataPointSummary", "dataPoin

- 用户3：确认
- 实际工具：collect_container_data；待确认：True
- 结果：[{"code": "expected_verification_not_finished", "detail": ["collect_container_data"], "severity": "major"}, {"code": "wrong_planned_or_submitted_values", "detail": {"expected": {"温度": 25}, "actual": null}, "severity": "major"}]
- 回复：⚠️ 安全确认：当前任务累计涉及 0 条创建、2 条修改、0 条删除，其中“批量导入/路线重建（影响数量需核对）”需要确认。操作已暂停且尚未继续执行。 数量是工具调用的估计影响量；批量导入或重建需另行核对完整范围。  请回复明确指令（例如“确认创建”“确认修改”或“确认删除”）继续，或回复“取消执行”。  评测夹具中的待确认计划（未调用MES）：[{"tool": "verify_collection_submission", "arguments": {"plan_id": "ae0af6db5166ac9c4b2af1ba73abf3d2", "plan_digest": "1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093"}, "plan": {"plan_id": "ae0af6db5166ac9c4b2af1ba73abf3d2", "plan_digest": "1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093", "kind": "collection", "state": "unknown", "body": {"container": {"name": "SN-EVAL-001", "instanceId": "0000000000000101"}, "dataCollectionDef": {"name": "质检采集", "revision": "1", "instanceId": "0000000000000201"}, "parametricData": {"@odata.type": "#shopfloor.DataPointSummary", "dataPointDetails": [{"dataPoint": {"name": "温度", "instanceId": "0000000000000501"}, "dataValue": "25"}]}, "comments": "MCP-COL:ae0af6db5166ac9c4b2af1ba73abf3d2"}, "arguments": {"container_name": "SN-EVAL-001", "name": "质检采集", "revision": "1", "values": {"温度": 25}, "collection_kind": "user"}, "submitted": false, "executable": true, "confirmation_required": true, "request_preview": {"body": {"container": {"name": "SN-EVAL-001", "instanceId": "0000000000000101"}, "dataCollectionDef": {"name": "质检采集", "revision": "1", "instanceId": "0000000000000201"}, "parametricData": {"@odata.type": "#shopfloor.DataPointSummary", "

- 用户4：再查它的普通采集历史
- 实际工具：无；待确认：True
- 结果：[{"code": "missing_expected_tool", "detail": ["get_container_collection_history"], "severity": "major"}]
- 回复：⚠️ 安全确认：当前任务累计涉及 0 条创建、2 条修改、0 条删除，其中“批量导入/路线重建（影响数量需核对）”需要确认。操作已暂停且尚未继续执行。 数量是工具调用的估计影响量；批量导入或重建需另行核对完整范围。  请回复明确指令（例如“确认创建”“确认修改”或“确认删除”）继续，或回复“取消执行”。  评测夹具中的待确认计划（未调用MES）：[{"tool": "verify_collection_submission", "arguments": {"plan_id": "ae0af6db5166ac9c4b2af1ba73abf3d2", "plan_digest": "1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093"}, "plan": {"plan_id": "ae0af6db5166ac9c4b2af1ba73abf3d2", "plan_digest": "1cd958dda310a7dbd2de4782b9d174d047a77fdc0a066cd3501667b01c185093", "kind": "collection", "state": "unknown", "body": {"container": {"name": "SN-EVAL-001", "instanceId": "0000000000000101"}, "dataCollectionDef": {"name": "质检采集", "revision": "1", "instanceId": "0000000000000201"}, "parametricData": {"@odata.type": "#shopfloor.DataPointSummary", "dataPointDetails": [{"dataPoint": {"name": "温度", "instanceId": "0000000000000501"}, "dataValue": "25"}]}, "comments": "MCP-COL:ae0af6db5166ac9c4b2af1ba73abf3d2"}, "arguments": {"container_name": "SN-EVAL-001", "name": "质检采集", "revision": "1", "values": {"温度": 25}, "collection_kind": "user"}, "submitted": false, "executable": true, "confirmation_required": true, "request_preview": {"body": {"container": {"name": "SN-EVAL-001", "instanceId": "0000000000000101"}, "dataCollectionDef": {"name": "质检采集", "revision": "1", "instanceId": "0000000000000201"}, "parametricData": {"@odata.type": "#shopfloor.DataPointSummary", "

