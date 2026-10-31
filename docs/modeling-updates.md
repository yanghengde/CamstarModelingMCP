# 建模字段更新契约

2026-10-31：将现场验证的 Spec 更新方式固化为工具实现、实际 MCP 参数定义及两个推理引擎共享的调用规则。历史对话里的成功操作不会自动改变程序；本次修改须重启 Python 服务才生效，外部 MCP 客户端也需刷新工具定义。

## 调用方式

先查询目标及版本，从真实返回取得 InstanceID。已有对象的普通修改使用实例路径 JSON PUT，只发送用户明确要改的字段：

```python
await update_spec(
    key=spec_instance_id,
    body_json='{"allowReprocess":true}',
)
await get_spec(key=spec_instance_id)
```

`update_spec`、`update_operation`、`update_product`、`update_workflow`、`update_mfgorder`、`update_mfgline`、`update_producttype` 及各自 OData 键变体仅将 key 设为必填。原有具名参数和位置调用保持兼容，但只有明确传入的字段进入请求体。创建工具仍要求原有创建参数；不将创建必填条件套用到普通字段更新。修改工单 Product 引用的具名参数须同时提供产品名称和版本。

不要将 GET 完整对象回传，也不要补全 null、false、空列表或 ReplaceDetails。省略具名参数表示不提交；body_json 中明确的 null 按字段可空契约保留，可能表示清除/解除引用。集合明细仍须遵循其包装与动作语义，不能把普通空列表当作安全的“保持原状”。

## 同类问题的统一处理

- `core/modeling_payload.py` 从已提交的七类 Swagger 写入 schema 规范化顶层和嵌套字段，包括 `AllowReprocess → allowReprocess`、引用内 `InstanceId → instanceId` 和 `UseRor → useROR`。未知、只读、重复字段及不符的基础 JSON 类型在发送前拒绝；不会静默丢掉整对象里的字段。
- 根对象 `UseRor` 与引用内 `useROR` 含义不同。根字段即使出现在部分 Swagger 中，也不属于本工具普通更新范围；此前整对象 PUT 收到无效 `__useROR` 节点错误，单字段 PUT 则成功。具体服务端转换过程没有调试证据，不据此断言必须改用 XML。根 `ObjectToChange` 也拒绝，目标由路径 key 决定。
- 六个 `patch_*` 兼容入口直接拒绝，能力目录返回 disabled，模型默认不加载。集合 PATCH 可能涉及创建/升版，不能依据 HTTP 方法名称推断其为普通局部更新。现有专门维护计划中的已核实 PATCH 升版逻辑不受影响。
- `agent/prompts.py` 和共享 `ROUTING_INSTRUCTION` 要求两个引擎按实际 InstanceID 发送最小更新并回读，不再凭 `Invalid XML Node` 断言必须 XML。`SP-...` 编号和 `AllowReprocess` 等追问能够加载 Spec 工具。
- 写入成功响应和回读一致分别核实后才能报告验证成功。回读由 Agent 调用对应 get 工具；update 函数本身不会自动读回，也不会把 HTTP 成功包装成“已验证”。超时或结果未知只做只读核实，不自动重发。回读同值本身不证明本次写入的因果关系。

## 验证边界

本次现场证据：对 `SP-MOD-EOL-001` Rev A（`00071c8000000053`）发送 `PUT /api/Specs/00071c8000000053`，请求体仅 `{"allowReprocess":true}`，服务返回明确更新成功；随后 GET 回读仅 AllowReprocess 从 false 变为 true，其余返回字段保持一致。

其余六类对象本次只做离线参数、payload、兼容性、工具加载及禁用入口回归，没有为验证而修改 MES 数据。字段级持久化、复杂明细、多态子实体及各部署扩展仍需各自验收，不把 Spec 的一次成功泛化为全部对象/字段已现场验证。写入白名单依赖仓库随附 Swagger；部署新增字段需先同步并核实契约。

离线回归使用 `tests/test_modeling_updates.py` 和 `scripts.offline_test_guard`，以 MockTransport 复现 Spec 的 JSON PUT 与回读，验证未改字段保持、超时单次写入及错误参数零网络请求。

在项目虚拟环境中运行，例如：

```powershell
.\venv\Scripts\python.exe -m pytest -q -p scripts.offline_test_guard tests/test_modeling_updates.py tests/test_mfgorders_payload.py tests/test_capability_discovery.py tests/test_tool_enhancements.py
```

项目要求 `aiosqlite>=0.20.0,<0.22.0`。本机全局 Python 的 0.22.1 与现用 LangGraph SQLite 检查点实现不兼容；本项目 venv 的 0.21.0 符合要求。应使用项目虚拟环境验证和启动，不以全局环境的依赖错误归因于本次建模更新。

本轮扩大回归在项目 venv 中完成：141 项测试及 2 个子测试通过，包含建模更新、原调用兼容、提示与工具路由、能力发现、写入策略、Workflow、MCP 传输及 LangGraph 检查点。使用网络拦截插件且未触发真实 MES 网络请求。Windows 默认 pytest 临时目录存在旧链接访问错误，本轮使用新建的独立 `--basetemp` 目录完成测试。
