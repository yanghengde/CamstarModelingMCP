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
