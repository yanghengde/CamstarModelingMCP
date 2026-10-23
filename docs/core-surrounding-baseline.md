# T00 现状核对与续接说明

核对日期：2026-10-23。阶段状态：T00 完成。首次连接超时的记录保留如下；后续实时复核见文末，以最新复核结果为准。

## 已完成证据

- 实际调用 `mcp.list_tools()`：注册 132 个工具；`TOOL_POLICIES` 132 项；缺失策略 0；缺失 `get_tool_func` 分发 0。
- `tests/test_capability_discovery.py` 与 `tests/test_agent_safety.py`：10 passed。
- 执行 `scripts/audit_surrounding_contracts.py`：15 个 Swagger GET 均 ConnectTimeout，未获得 HTTP 响应；不能据此判断接口不存在或没有权限。
- 从已有本地缓存归档 CollectData、ExecuteMfgOrdertask 两个完整定义，去除服务器地址及示例数据，保存文件 SHA-256。缓存采集时间没有独立证明，不能将本次归档日期视为原始采集日期。
- 机器可读结果：[契约证据](core-surrounding-contract-evidence.json)。剩余 13 个分组无可归档的本地完整定义。未调用业务写入接口，也未创建样本。

## 可复用代码与真实缺口

| 入口 | 已有能力 | 后续开发注意事项 |
|---|---|---|
| `tools/process_context.py::ReadContext.get/resolve` | 有界 GET、按 ID/名称版本/ROR 解析、唯一性和引用一致性检查 | 现有解析假设响应字段使用 PascalCase；新增对象必须以实际响应验证 |
| `tools/modeling_details.py::read_envelope` | 保留业务字段、分页、nextLink、缺失导航与完整性提示 | null 不等于未配置；本地分页不等于服务端全量分页 |
| `tools/modeling_details.py::get_spec_details/get_mfgorder_details` | Spec 引用、工单物料/任务详情 | 可复用，不另建同义工具；新的导航展开大小写需要验证 |
| `tools/process_context.py::get_product_process` | 产品到工艺链的有界遍历 | T04 聚合时复用，不能绕过其完整性边界 |
| `tools/order_progress.py`、容器 inquiry/trace/genealogy | 工单进度、容器状态与追溯 | 可做后续事务回读基础，但新增事务的历史字段仍待现场确认 |
| `core/http_client.py` | 鉴权、服务分流、GET 重试与错误转换 | 不把字符串错误交给成功数据解析，不自动重放不确定写入 |
| `core/tool_catalog.py`、`tools/__init__.py` | 显式策略与执行分发 | 每个新增工具同时注册策略、导入模块和分发 |
| `agent/tool_routing.py`、`tools/tool_discovery.py` | 按领域加载、能力目录 | 当前没有独立 resources/material/data_collection 分组；T01 起必须同步分组枚举、标签、关键词及加载说明，或明确复用已有分组 |

当前缺少专用的资源/资源组、BOM、采集定义查询与准备检查工具；也缺少本路线规划的投料、采集提交、拆合批/关联写入、工单任务执行、文档集查询工具。通用 Query 或 body_json 入口不等同于这些领域能力已完成。

## 已归档契约的可确认事实

| 分组 | 请求引用 | 返回/错误边界 |
|---|---|---|
| CollectData | required=container；dataCollectionDef→RevisionedObjectRef；parametricData→ParametricData | 主事务 POST 的 responses 仅列 200；不能据此推断全部错误格式或无业务错误 |
| ExecuteMfgOrdertask | required=mfgOrderProcedure,mfgOrderTask；过程→RevisionedObjectRef；任务→NamedSubentityRef；状态→NamedObjectRef | 同样仅声明 200；权限、重复执行和状态限制尚未验证 |

Modeling 的 key、ROR、引用结构、`$select`、`$expand`、分页，以及业务错误格式目前只在既有实现中有使用证据，**尚未对新增分组现场验证**。不能将现有对象的成功行为直接外推到新对象。

## 样本与限制

所有新增 Modeling 分组本次均未读取样本。状态是“连接失败，样本未知”，不是“空列表”或“未配置”。未请求 Shopfloor 事务作为样本探测。

连接恢复后：

1. 在仓库根目录运行 ` .\venv\Scripts\python.exe scripts/audit_surrounding_contracts.py`；脚本只执行 GET，每次请求超时 12 秒，并发上限 4。
2. 检查证据中 provenance=live 的分组；脚本对 Modeling 仅探测 `$top=1`，保存数量和字段名，不落业务值。
3. 从实际响应选择可定位的只读样本，以实例 ID 验证详情，再逐项验证 select/expand、skip/top、版本/ROR 与关联字段。按需核对 `$metadata`，不要猜导航名。
4. 保存去敏验证结果及可复查的样本定位方式；没有样本时明确标记，不自动建数据。
5. 补齐 T00 的契约/样本复核清单后再标记完成，并进入 T01。

本阶段没有新增工具，因此无需重启正在运行的服务。T01 的工具实现与现场验收尚未开始。


## 2026-10-23 续接：实时复核完成

现场连接已恢复。重新运行契约审计，15 个分组均 HTTP 200、provenance=live，完整脱敏契约已保存至 `Swagger/extensions/`。原先两份缓存已由本次实时契约替换。审计最初的 ResourceGroups / DataCollectionDefs 样本查询超时，随后独立只读复核均成功；两份证据保留各自观测结果，不抹去失败记录。

新增脚本：`scripts/validate_surrounding_reads.py`。结果：[只读验证证据](core-surrounding-read-validation.json)。

- Modeling `/api/$metadata` HTTP 200；已解析 BaseType 继承，八类实体主键均为 InstanceID。证据保存继承后的属性/导航及元数据 SHA-256，避免只看派生实体而遗漏键或导航。
- 37 次只读检查：35 次 HTTP 200；2 次 HTTP 400，分别为 ESigHistoryDetails 展开限制和主动读取不存在对象的错误格式探测。没有写入。
- Resources、ResourceGroups、BOMs、MfgOrderTaskLists、DocumentSets、DataPointCollectionDefs 列表和计数均成功，在当前账号可见范围内为 0。不能推断其他账号也没有数据；非空详情、关联、拆分页和嵌套资源组仍待后续阶段样本验收。
- DataCollectionDefs 与 UserDataCollectionDefs 均有 2 条记录；证据保存一个样本的 InstanceID 及名称/版本过滤条件，用于下次复核。已取得按 ID 读取且 ID 匹配、名称+版本、IsRevOfRcd、top/skip 和 select 的成功响应。
- UserDataCollectionDefs 的 DataPoints 展开成功。DataCollectionDefs 的 ESigHistoryDetails 在 metadata 中存在，但实际 `$expand` 返回 400；不能宣称元数据中的全部导航都可展开。后续仅使用验证通过且业务需要的导航，对拒绝展开返回明确限制。
- 缺失对象返回 HTTP 400，响应为 ErrorCode/Message 等字段；被拒绝的展开为 error 包装。不能仅按 404 或单一错误形状处理。
- Swagger 集合 GET 未列出 OData 查询参数；本次 `$metadata` 与实际 GET 是 select/expand/filter/paging 的补充依据。未在新增事务上验证权限、现场状态、重复执行或完整业务必填规则。
- 再次运行能力发现/安全回归：10 passed。新工具仍未实施，现场事务验收不适用。

T00 验收结论：契约、已有代码覆盖、可用样本和样本不足范围已落盘，满足进入 T01 的前置条件。下一阶段先实现资源只读工具；空样本相关场景先用契约驱动测试验证，并明确保留非空现场验收项，不为了测试自动创建资源。
