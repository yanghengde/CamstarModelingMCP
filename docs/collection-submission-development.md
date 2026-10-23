# T06 数据采集提交开发记录

## 已实现（2026-10-23）

`validate_collection_values(values, name, revision, instance_id, use_ror)` 已接入工具注册、collection 分组和动态能力列表。

- 根据明确 ID 或名称版本/ROR 读取 UserDataCollectionDef；校验前要求定义完整、点名唯一。
- 检查必填、未知点名、整数/数值/字符串/布尔类型，拒绝非有限数值，不自动把字符串转换为数字。
- 对数值常量上下限分别报告 within / outside / unknown。动态表达式不求值，null 不视为无限制。
- Object、TimeStamp 的格式契约尚未验证，报告未知；不支持分组采样、单位换算或枚举校验。
- static_valid 仅表示已支持的静态必填和类型检查结果。complete=false、executable=false、quality_verdict=null；越限仅作提示，不能当作最终提交许可。

现场只读验证：SPCTesterPageUDC / 1 的 Thickness 使用示例值 1 检查，static_valid=true。示例值未提交；限值、单位和动态规则仍未知。结果见 [证据](collection-values-validation.json)。

## 未完成与下一次顺序

1. 已归档 `Swagger/extensions/Shopfloor-CollectData.json` 的 ParametricData 只包含 displayName、instanceID、parent、smartBarcode、historyDetail，不能据此猜测采集值载荷。取得该定义对应的具体 parametricData 类型、动态字段及现场客户端成功请求样例（脱敏）。
2. 验证定义到具体类型的关联、值序列化、越限与覆盖规则、分组采样格式，再实现可审阅的真实提交预览。
3. 提交前重新读取容器身份、当前工序、事务采集映射和定义版本，通过既有写入确认机制执行 collect_container_data。当前不注册虚假的执行工具。
4. 验证采集历史表/Query 接口及关联事务标识，实现准确回读。超时不自动重试，先核实是否已经提交。
5. 增加工序变化、重复提交、响应丢失、部分失败测试，并用明确的测试容器完成现场验收。

没有进行任何 Shopfloor 写入。T06 尚未完成端到端验收。

验证：39 项相关测试通过（校验、采集定义、能力发现和 Agent 安全）；git diff --check 通过。


## 第二步：容器采集历史查询（2026-10-23）

新增 `get_container_collection_history(container_name, container_instance_id, txn_id, offset, limit)`，已接入 collection 能力组及动态能力列表。

先核对容器身份，再通过固定 SELECT 查询 DataPointHistoryDetail → DataPointHistory → HistoryMainline，返回原始采集值、类型、上下限、越限/覆盖标记、定义 ID 和事务 ID。显式 ID 仅接受 16 位十六进制；分页上限 100，额外读取一条判断下一页；支持已知 TxnId 精确筛选。身份不符、重复明细或查询错误时不返回成功页。

证据：[字段结构](collection-history-schema.json)、[现场只读验证](collection-history-contract.json)。真实接口已确认字段存在且查询可执行；当前 DataPointHistoryDetail 总数为 0，测试容器返回空页。关联路径仍需非空样本验证，因此 complete=false、nonempty_linkage_verified=false。page_complete 仅表示该页查询和身份检查通过，不表示覆盖所有采集类型。

该工具不覆盖自定义 ParametricData / 分组采样历史，不根据空页判断某次提交失败，不触发自动重试，也不把历史越限标记转换为质量结论。51 项相关测试通过（含分页、类型校验、身份不匹配、事务筛选、重复行和能力发现），未进行业务写入。

下一步仍是核实具体 UserDataCollection 提交结构及越限规则，随后实现确认后的提交和按事务标识回读；不能仅凭历史表结构反推请求字段。


## 第三步：服务预览与单次执行入口（2026-10-23）

用户决定：先完成不依赖现场写入的开发，T00–T09 后集中补现场测试。本次没有执行真实采集。

### 契约补齐

本地《OCEXCR REST API 2510+》1-23～1-28 明确 Swagger 不显示继承子类型，需要元数据和 @odata.type；3-2 明确 execute=false 不添加 Execute 节点。实时 Shopfloor/api/$metadata 确认 DataPointSummary 派生于 ParametricData，包含 DataPointDetails，每项必须有 DataPoint 引用，DataValue 是字符串。服务以 execute=false 接受并回显了 Thickness=1，类型 7273/7276 与元数据一致。

证据：[元数据](collection-submit-metadata.json)、[加载](collection-load-validation.json)、[序列化](collection-serialization-validation.json)、[完整预览](collection-preview-validation.json)。完整预览的 server_echo_verified=true，但测试容器当前 Spec 未返回，因此 static_mapping_verified=false、executable=false，没有生成执行计划。

### 工具与行为

- preview_container_data_collection：静态校验、工序 CollectData 映射、明确版本和点ID、服务不执行回显；仅数值/字符串。不自动转换单位，不设置覆盖限值或绕过必填字段。
- 关联及回显通过才保存不可变、有效1小时的计划。摘要绑定服务环境、容器、工序、定义、值和已返回约束。
- collect_container_data：写工具，always_confirm=true，确认界面展示完整提交内容；执行前再次预览，比对摘要，原子领取单次计划后才提交，不自动重试。
- get_collection_submission_plan：读取计划与持久化结果。异常/超时/崩溃后计划不能重新领取；同一环境容器存在 in_flight/unknown 时禁止另一计划绕过。
- 提交后读取采集历史作证据。当前契约尚不能可靠关联本次响应与历史事务，**即使收到 HTTP 200，结果仍为 unknown、verified_success=false**，不报告核实成功，也不自动解除容器的未知状态。核实/恢复入口待现场契约验证后实现，当前需人工处理。

### 尚未完成

端到端 T06 不勾选完成：布尔/日期/对象及分组采样未支持；非空历史关联和真正成功判定尚未完成；未知结果核实后解除锁定的流程待补。写入口已实现并通过模拟验证，不能混同现场验收完成。统一补测见 [集中验收清单](core-surrounding-deferred-tests.md)。

本轮验证：66 项相关测试通过，包含确认展示、工序变化拒绝、摘要/过期、并发单次领取、超时持久化及新计划防绕过；git diff --check 通过。pytest 退出时有 Windows 临时目录清理权限提示，测试结果与退出码正常。
