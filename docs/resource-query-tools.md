# T01 设备资源与工序资源查询

2026-10-23：实现与模拟验证完成；现场空列表、缺失对象及 Spec 缺少资源组引用的读取通过。非空资源、资源组及 PMStatus 展开尚无现场样本，未标记为现场全量验收通过。

## 工具

| 工具 | 参数与用途 |
|---|---|
| `list_resources` | 可选精确 name；offset>=0、limit=1..100。按 Name/InstanceID 排序，多取一条探测下一页。检查 complete、has_more、next_offset。|
| `get_resource_details` | 名称或 instance_id；可选 include_maintenance 展开 PMStatus。保留原始配置，返回 Availability 原值及 Swagger 定义的 1=Up、2=Down；其他值为未知。|
| `get_spec_resource_context` | Spec 名称+明确 revision 或 instance_id；max_groups=1..100、max_resources=1..200、max_depth=1..20。解析 ResourceGroup、Entries、Groups，读取候选资源，去重并报告循环。|

均为只读 GET，新增 `resource` 工具分组，已同步策略目录、模块分发、能力目录、显式加载枚举和自然语言路由。无写入能力，无需业务确认。

示例：“查询设备资源列表”“查看设备 R001 的配置”“查询 Spec A、版本 1 配置的资源”。名称不唯一时不会任选对象；无 ID 且未给 Spec 版本时返回提示，不默认猜测 ROR。

## 结果边界

- 配置候选不等于允许开工的设备；不执行派工、资质或设备实时运行校验。
- null/缺失引用不等于业务未配置；空 Entries/Groups 与字段未返回区分处理。
- 资源组 Entries/Groups 是引用集合，不作为 OData 导航展开；PMStatus 是 metadata 声明的导航，但本环境非空展开仍待验收。
- 重复资源去重；资源组循环、深度/数量/请求预算、后续页和局部失败均使 complete=false，保留已获取结果。
- 资源详情保留父子资源引用，不递归读取设备层级；维护状态按需读取，失败时保留基础详情。
- Availability 只是产品状态字段，不自动推导生产就绪结论。

## 验证

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_resources.py tests/test_capability_discovery.py tests/test_agent_safety.py tests/test_process_context.py -q
```

46 passed。覆盖分页、空页、错误、歧义名称、ID 不匹配、未知字段、嵌套组、去重、循环、上限、缺失引用、维护展开失败、注册、显式分组加载和自然语言路由。测试复用实际 ReadContext，HTTP 层使用模拟响应。

现场证据：[resource-query-validation.json](resource-query-validation.json)。真实读取资源列表为空；不存在资源返回 400 并被工具标记不完整；现有 Spec `Incoming Inspection` 版本 1 可读取，但 ResourceGroup 未返回/为空，工具不声称配置为空或设备可开工。没有创建样本或执行业务写入。

未完成验收：非空资源详情、嵌套组实际序列化、PMStatus 展开、自然语言聊天端完整回合。路由选择及 MCP 注册已用测试验证，不能替代浏览器端和非空现场验收。

部署：重启应用后刷新客户端工具清单，新进程才会加载三个工具。本轮未擅自重启服务或中断已有对话。下一阶段 T02 可继续；上述现场验收项应持续保留。
