# T03 数据采集定义与静态工序要求

2026-10-23：两个只读工具已实现，用户采集定义非空读取已现场验证；容器动态要求、分组点非空与聊天端验收仍待补。

## 工具

- `get_data_collection_details`：ID、名称+版本或显式 use_ror 定位；kind=user（默认）展开 DataPoints；kind=point 展开 Groups/Points；kind=base 仅显示基础定义，不猜子类型。offset>=0，limit=1..200；分组中的点也限制为每组 limit 条，超限标记不完整。
- `get_container_collection_requirements`：复用固定只读 ContainerInfoInquiry，核对容器身份，再读取 CurrentSpec → TxnMap → DataCollectionDef。可按 transaction_name 精确匹配 TxnName，max_definitions=1..30；definition_kind 默认 base，可明确 user/point 后进一步展开。无 Shopfloor 写入。

新增 `collection` 分组，已同步注册、策略、分发、能力发现、显式加载与自然语言路由。示例：“查看用户采集定义 SPCTesterPageUDC 的 1 版”“查看容器 SN0000001 的 MoveOut 静态采集配置”。

## 契约与结果语义

- Spec 没有本次已验证的直接 DataCollectionDef 字段，关联来自 TxnMapChanges.dataCollectionDef；Required、Sequence、TxnName 是事务映射字段，不能代替点自身的 IsRequired。
- DataPointChanges 的 DataType 枚举来自 Swagger：1 Integer、2 Float、3 Fixed、4 String、5 Object、6 TimeStamp、7 Boolean、9 Decimal。其他值返回未知，不推测为枚举。
- 用户点保留 IsRequired、UOM、LowerLimit、UpperLimit 及原始项。null/缺失明确列入 unknown_constraints；false 和数值0不能丢失。
- 分组点依据 `$metadata` 的 Groups → Points 导航读取，区分内层警告限值 InnerLower/UpperLimit 与外层规格限值 OuterLower/UpperLimit，并保留 MinimumSamples。限值可能是字符串表达式，仅展示，不计算或执行。
- 统一枚举选值来源尚未确认，allowed_values 明确为 unknown。constraints_complete 始终为 false；静态列表读取完整不代表所有约束或动态必填已掌握。
- 容器工具 overall complete 始终为 false，dynamic_requirements_verified=false；static_mappings_complete 单独描述静态映射的读取完整性。不匹配映射不等于无需采集。
- 缺少当前 Spec 返回 current_spec_unknown，停止关联推导；不给容器随意套用某个采集定义。名称/版本歧义、展开失败、后续页、部分定义错误与输出上限均显式报告。
- 尚不执行 CollectData，也不输出采集合格或批次质量合格结论。

## 验证与待办

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_data_collection.py tests/test_boms.py tests/test_resources.py tests/test_capability_discovery.py tests/test_agent_safety.py tests/test_process_context.py -q
```

69 项相关测试通过，包含数字/文本/布尔/未知类型、0与false、分页与缺失字段、空列表、版本定位、展开身份校验、组内点、原始限值表达式、容器身份、映射过滤与边界、注册和路由。

现场证据：[data-collection-validation.json](data-collection-validation.json)，包含相关元数据类型、字段与响应摘要。

- SPCTesterPageUDC/1 返回 Thickness 点，DataType=3、IsRequired=false，上下限与单位为 null，按未知展示。
- SN0000001 的固定 Query inquiry 返回身份，但 CurrentSpec 未返回；工具返回 current_spec_unknown，没有猜测关联。
- 非空 DataPointCollectionDefs 分组点、具备 CurrentSpec/TxnMap 的完整容器关联、枚举选值与动态规则、聊天端完整问答仍需验收。

部署需重启服务并刷新客户端清单，本轮未重启服务。下一阶段 T04 可复用此处静态结果，但必须把未知动态规则和缺少当前工序的检查项列为“未知”，不能计为通过。
