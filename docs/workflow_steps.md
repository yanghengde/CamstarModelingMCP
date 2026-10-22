# Workflow Step 查询

## 已核实的接口契约（2026-10-22）

从配置的 Modeling 服务读取 `/swagger/index.html`，Workflow 相关分组包含
`Workflows` 和 `WorkflowMaint`，没有独立的 Step 分组。最新的
`/swagger/Workflows/swagger.json` 已保存至
`Swagger/modeling-workflows-live-swagger.json`，其中包含 `StepChanges`、
`SpecStepChanges`、`MovePathChanges` 等 Schema。

实时 `/api/$metadata` 声明：

- `WorkflowEntity.Steps` 是指向 `StepChanges` 的集合导航属性。
- `Steps-Expanded` 是普通属性，不是 `$expand` 的导航名称。
- 步骤的 `Paths` 导航返回路径，路径包含 `ToStep` 和 `IsDefaultPath`。

已验证的只读请求：

```text
GET /api/Workflows/{InstanceID}?$expand=Steps
GET /api/Workflows/{InstanceID}?$expand=Steps($expand=Paths)
```

本环境展开嵌套 Paths 时，Steps 上的 Spec、Operation 等字段会返回 null；
只展开 Steps 时可以取得这些引用。因此工具执行两次读取，在相同 Workflow
名称和版本内按精确 Step 名称关联。两次读取中的 Changes CDO ID 可能不同，
不能按临时 ID 关联。步骤集合或版本不一致时保留步骤并警告，不拼接路线。
这不是事务快照；查询期间应避免同时修改路线。

## MCP 工具

- `list_workflow_steps(workflow_id)`：返回步骤、实际 Spec/Operation 引用、普通路径和标准路线。
- `get_workflow_step(workflow_id, step_name)`：查询该路线中的指定步骤。

`workflow_id` 使用 `list_workflows` 返回的 InstanceID。Step 名称不等于 Spec 名称。
`standard_route` 从唯一首步骤沿默认路径排列，全部步骤保留在 `steps` 中，
分支不会被丢弃。没有普通出路径且没有 DefaultPath 的节点视为本次路径图的终点。
循环、多默认路径、目标缺失、分页和读取失败均返回警告；使用者需检查
`steps_complete`、`standard_route_complete` 和 `warnings`。

本工具不查询返工路径或条件选择器，不执行任何维护、保存或重建操作。
响应使用专用 JSON 序列化，避免通用 key-field 裁剪删除 Steps/Paths。

## 验证结果

- `WF-WM-900A / A`：6 步，冲压 → 焊接 → 喷涂 → 总装 → 测试 → 包装。
- `WF-PACK-01 / A`：13 步，从 `010-CELL-INSP` 到 `130-PACK-STORE`。
- 关联 Spec 从 Step.Spec 直接读取，未使用名称或业务常识推断。
- 单元测试覆盖实际关联、分支、循环、多默认路径、分页、读取不一致、错误与工具注册。

重启 Agent/MCP 服务后加载新工具；提示词已指引助手在查询路线步骤时优先使用此工具。
