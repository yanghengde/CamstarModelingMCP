# T04 开工准备静态配置检查

2026-10-23：核心汇总实现完成，真实工单只读检查通过；完整现场配置及聊天端验收仍待补。

工具：`check_mfgorder_readiness(mfgorder_name, max_specs=5, max_depth=2)`，注册到 mfgorder 分组。示例：“检查工单 MO-100076435 的开工准备配置”。

## 检查范围

1. 唯一定位工单及产品版本；失败时停止推导。
2. 复用 get_product_process 与 inspect_structure 检查产品工艺结构和引用。
3. 复用 get_product_material_requirements 分别检查标准 BOM、工单物料的读取完整性。
4. 对已解析 Spec 复用资源上下文查询，并读取 TxnMap 静态采集映射。
5. 显示库存、资质、设备实时状态、动态采集要求等未接入项。

每项带 area、object、status、source、detail、next_action。允许状态为“通过 / 缺失 / 未知 / 不适用”；当前没有证据可靠判断缺失或不适用时，只使用通过与未知。空资源/物料清单不能自动判缺失，因为尚未确认现场是否要求必须配置。

“通过”仅说明该项所述的关联/清单读取或已检查静态结构通过，不代表库存充分、数量合理或设备可用。动态条件未验，整体 complete=false、ready_to_start=null。不会执行 Start，不会把静态检查当作开工许可。

## 边界与成本

- max_specs=1..10，max_depth=0..3；超过工序检查上限的 ID 单独列入 unexamined_specs。
- 工艺读取沿用160个逻辑请求预算、最多20条路线及每路线200步骤；工序资源限制5组、10资源、深度3。
- 并发最多2个领域/工序；总逻辑请求上界为178+33×max_specs（最大508），来自各领域独立预算之和；HTTP GET 的内部重试可能使网络尝试数翻倍。不是原子快照。
- 部分领域失败保留其他检查证据。大输出沿用80,000字符上限，超过时明确报错。
- 当前采集检查只汇总 TxnMap 可读性与数量，所有采集项保持未知；并未逐个展开定义与全部点约束。这是后续增强项，不宣称采集要求已完整验证。

## 验证

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_production_readiness.py tests/test_data_collection.py tests/test_boms.py tests/test_resources.py tests/test_capability_discovery.py tests/test_agent_safety.py tests/test_process_context.py -q
```

75项相关测试通过。新增覆盖静态完整但仍不能开工、部分接口失败、空资源、不完整检查范围、歧义对象、参数边界、重复读取一致性和注册路由。

现场证据：[production-readiness-validation.json](production-readiness-validation.json)。工单 MO-100076435；max_specs=2、max_depth=1；2项通过、8项未知、4个工序未检查。仅执行 GET，无业务写入。证据记录运行时参数，不回填后续新增的展示字段。

待办：全配置现场样本、全部工序范围、逐采集定义约束汇总、聊天端完整回合。T01–T03 既有样本限制继续有效。部署后重启服务并刷新客户端工具清单；本轮未重启运行服务。
