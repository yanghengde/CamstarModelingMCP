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
- 并发最多2个领域/工序；总逻辑请求上界为178+(33+3×max_definitions)×max_specs（最大808），来自各领域独立预算之和；HTTP GET 的内部重试可能使网络尝试数翻倍。不是原子快照。
- 部分领域失败保留其他检查证据。大输出沿用80,000字符上限，超过时明确报错。
- 采集检查逐条解析 TxnMap 关联定义，汇总点/组、类型、必填、单位、限值与未知约束。默认 collection_kind=base，仅在引用 CDOTypeName 明确为已支持子类型时自动展开；可显式指定 user/point。max_definitions=1..10（默认5）、point_limit=1..50（默认20）。重复定义在同一工序内复用；类型未知、分页与部分失败保持不完整，动态采集要求始终未知。

## 验证

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_production_readiness.py tests/test_data_collection.py tests/test_boms.py tests/test_resources.py tests/test_capability_discovery.py tests/test_agent_safety.py tests/test_process_context.py -q
```

75项相关测试通过。新增覆盖静态完整但仍不能开工、部分接口失败、空资源、不完整检查范围、歧义对象、参数边界、重复读取一致性和注册路由。

现场证据：[production-readiness-validation.json](production-readiness-validation.json)。工单 MO-100076435；max_specs=2、max_depth=1；2项通过、8项未知、4个工序未检查。仅执行 GET，无业务写入。证据记录运行时参数，不回填后续新增的展示字段。

待办：全配置现场样本、全部工序范围、聊天端完整回合。T01–T03 既有样本限制继续有效。部署后重启服务并刷新客户端工具清单；本轮未重启运行服务。


## 采集汇总补充验证

2026-10-23：78项相关测试通过。新增定义去重、子类型证据、数量上限、原始限值保留、未知子类型和局部失败验证。再次只读检查真实工单返回10项检查；现场所查工序没有非空采集映射，不能声称已完成完整工单→点链路现场验收。证据：[采集汇总验证](production-readiness-collection-validation.json)。

## 可选任务及文档配置摘要（2026-10-24）

check_mfgorder_readiness新增include_tasks=false、include_documents=false和documents_required=null。默认不开启额外读取；明确要求文档必需性时须同时include_documents=true，非布尔值拒绝。工具注册的完整schema及能力说明已同步。

include_tasks复用get_mfgorder_tasks的首20项候选及过程、分页、完整性、工单ID核对；只显示候选引用，不执行任务或将任务状态当作产量。读取不完整/失败或身份冲突为未知。include_documents仅对max_specs范围内工序查询首20项文档元数据，不下载文件。documents_required未指定始终不据空关联判缺失；明确true且完整读取确认无关联才标缺失，false且读取完整则标不适用。有效关联只通过配置检查，不证明文件可访问。

两类查询均保留来源、局部错误与未检查范围，ready_to_start始终null、整体complete仍false。并发不超过2个领域/工序；在原逻辑请求预算上追加任务52、每个文档工序46的保守预算。

新增测试覆盖默认不读取、候选分页、工单ID冲突、三种文档要求、部分权限失败、工序上限、参数拒绝及失败保留其他证据。隔离工单CDX-T05-20261024-B-MO的只读实测结果3项通过/7项未知，未配置过程、文档业务要求未指定；未执行Start/任务/文档下载，见[现场证据](t05-fixtures/readiness-with-tasks-documents.json)。非空任务/文档与聊天端仍需验收。
