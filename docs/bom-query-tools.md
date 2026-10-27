# T02 BOM、产品用料与版本比较

2026-10-23：实现与模拟验证完成，非空现场及聊天端验收待补。三个工具均只读 GET，不创建 BOM，不执行投料。

## 工具与输入

| 工具 | 用途 |
|---|---|
| `get_bom_details` | name+revision、instance_id 或 name+use_ror=true 定位 BOM，展开 MaterialList；offset>=0、limit=1..200，默认50。 |
| `get_product_material_requirements` | 使用相同定位规则读取产品及标准 BOM；可选 mfgorder_name 单独显示工单 MaterialList，先核对产品实例/版本一致。 |
| `compare_bom_versions` | 两侧明确 name+revision，按唯一明细 Name 比较新增、删除及字段变化；每侧最多200条，不接受缺页或不唯一的行标识。 |

显式 use_ror 不能与版本或 ID 混用；无 ID、无版本且没有显式选择 ROR 时不猜当前版本。产品 BOM 引用若明确 UseROR，沿该引用解析，复用 ReadContext 的唯一性、版本和 ID 一致性检查。

示例：“查看 BOM B 的 A 版用料”“查询产品 P 的 1 版标准 BOM，同时展示工单 O 的用料”“比较 BOM B 的 A、B 两个版本”。

## 契约依据与边界

- `Swagger/extensions/Modeling-BOMs.json` 及 T00 的继承后 `$metadata`：BOM 主键 InstanceID，MaterialList 是导航；MaterialListItem/ProductMaterialListItem 为具名子实体。明细 Name 是组内匹配键，不使用不同版本的 InstanceID 或数组位置匹配。
- 同一物料可以出现在不同明细名称中；重复物料不会被错误合并。缺失/重复明细名称时拒绝比较，改名按删除+新增展示。
- 比较范围是读取到的 MaterialList 字段，排除行 InstanceID、Parent、DisplayName；不是全部 BOM 配置或业务等价证明。嵌套引用仍保留返回值，可能出现引用表示差异，应结合明细判断。
- 数量、单位、损耗、工序、替代料等按实际返回字段展示，不自创字段，不计算总需求，不做未经验证的单位换算或损耗计算，也不提供库存结论。
- 产品标准 BOM 与工单清单分别返回；不猜覆盖优先级。一个来源失败时保留另一个来源，并将整体 complete 标为 false。
- 复用 read_envelope 的分页/nextLink/缺失字段处理；null 字段按该公共函数省略，缺失不代表未配置。分页仅是单次服务端响应的本地片段，不冒充全部数据。
- 200条上限、后续页、重复名称或错误响应都会阻止完整差异结论。输出超过80,000字符时明确返回错误，不静默裁剪为完整结果。

## 集成与验证

新增 `material` 分组，已接入 `tools/__init__.py`、`core/tool_catalog.py`、`tools/tool_discovery.py` 和 `agent/tool_routing.py`。已验证注册、执行分发、能力目录、显式加载与自然语言路由。

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_boms.py tests/test_resources.py tests/test_capability_discovery.py tests/test_agent_safety.py tests/test_process_context.py -q
```

57 passed；新增11项 BOM 测试覆盖空列表、分页、nextLink、错误、ROR、参数冲突、展开对象不一致、工单产品不匹配、来源局部失败、排序变化、单位变化、重复物料、增删行、重复/缺失行名、输出范围及工具集成。

现场证据：[bom-query-validation.json](bom-query-validation.json)。BOM 列表 HTTP 200 且本次返回0条；读取现有产品 WM-900A/A 成功，但 BOM 引用无法解析，返回 complete=false；不存在 BOM 返回400，工具保留错误状态。查询到可见工单样本，但未声称其物料与该产品关联已验收。

待验收：非空 BOM 导航与明细结构、真实双版本差异、工单实际物料清单关联、聊天端完整问答。没有为填补样本自动创建业务数据。

部署后需重启服务并刷新 MCP 客户端工具清单；本轮未重启已有服务。下一阶段 T03 可继续开发，T01/T02 的现场待验收项继续保留。
