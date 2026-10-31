# ContainerLevel 完整建模工具

2026-10-31。补齐整个容器层级对象的维护能力，覆盖查询、完整字段结构、创建、更新、删除及字段选择值，不再仅维护 AllowMove。本轮只做程序开发、只读契约/数据格式核对和离线写入测试，没有修改 BOX、CARRIER 或任何容器的实际配置与状态。

## 工具与接入

8 个工具均在 reference 组注册，由实际 MCP 工具目录、应用执行分发、能力发现、按需加载和自然语言路由共同接入：

| 工具 | 能力 |
|---|---|
| list_container_levels | OData 过滤、分页、字段选择和展开 |
| get_container_level | 按名称或实例 ID 读取，支持 expand，保留字段不做关键字段裁剪 |
| get_container_levels_count | 按过滤条件计数 |
| get_container_level_schema | 返回全部字段目录；指定字段返回精确 schema 与递归引用类型 |
| create_container_level | 创建明确名称的层级，预先查同名，提交后解析唯一 ID 并回读 |
| update_container_level | 按16位真实 ID 更新明确字段，核对冻结状态并回读 |
| delete_container_level | 核实身份和冻结状态后单次删除，回读核对404；遵循应用既有删除确认策略 |
| request_container_level_selection_values | 按原生表达式和上下文读取候选值，不保存或自动选择 |

创建、更新、删除分别归类 create/update/delete，查询、字段结构和选择值归类 read。加载写工具仍需 include_writes=true；加载工具不等于获得业务授权。直接外部 MCP 调用沿用客户端授权，不声称具备应用内部相同的审批流程。

当前部署 Swagger 声明 GET/POST、按实例路径的 PUT/DELETE、RequestSelectionValues 及等价 OData 键路径；没有新增未声明的 PATCH。工具优先使用实际 InstanceID 路径，不重复暴露等价别名。

## 完整字段范围

归档契约共有36个根字段，31个声明为非只读，其中30个可作为本工具的显式配置输入。覆盖：

- 名称、AllowMove、IsNameUnique、描述、备注、FilterTags、IconId。
- ContainerNumberingRule、ParentLevels、ChildLevels、UIPreference。
- Attributes、WIPMsgConfig、WIPMsgDefMgr、ChangeHistory。
- InstanceLocked、IsAttachImage、AssociatedPackages 等契约声明的字段。
- ReplaceDetails、ReplaceDetailsSubentityListNames、SkipSubentityListsToSequence、UseROR/UseRORProxy/AttachAsRORProxy 布尔数组以及各声明的 Expanded 包装。

五个只读字段（包括 InstanceID、IsFrozen、DisplayName 和文档附加详情）不可写。ObjectToChange 虽出现在写入 schema 中，但属于目标选择器，不允许通过请求体改写路径目标，不计为业务配置输入。

常用字段提供具名参数；其余字段通过 body_json 使用同一完整 schema 校验。字段及嵌套引用按契约规范大小写，拒绝未知/只读/重复字段和类型错误，int32 校验范围。此对象的 useROR 是布尔数组，与 Spec 等修订对象的标量 UseRor 不同；不会统一错误地禁用合法数组。

省略字段保持原值；JSON null 是明确的清除请求。工具不会补默认 false、null、空列表、父子关系或编号规则，不自动修改被引用的对象。列表输入及 Expanded 动作必须依据原生契约，不把空列表或未核实动作字符串推断成删除语义。框架是否接受某种明细动作、是否替换或合并，以及复杂持久化语义仍由服务端契约和现场验收确定。

## 示例

```python
# 原 AllowMove 调用保留兼容。
await update_container_level(key=level_id, allow_move=True, expected_name="BOX")

# 多字段与引用维护；所有值均须来自用户明确意图/已查询配置。
await update_container_level(
    key=level_id, expected_name="BOX", is_name_unique=True,
    description="周转箱层级", container_numbering_rule_name="BOX-NUMBER",
    parent_level_names=["PALLET"], child_level_names=["UNIT"],
    body_json='{"FilterTags":"production","Notes":null}',
)

# 复杂字段先读取完整嵌套类型，不猜明细结构。
await get_container_level_schema(field_name="attributes-Expanded")
await request_container_level_selection_values(
    selection_values_expression="ContainerNumberingRule", body_json='{"Name":"BOX"}',
)
```

更新允许显式 name 重命名，expected_name 始终核对旧名称；未指定重命名时从现有对象读取 Name 以满足当前 PUT 契约。创建中的 body_json.name 不得改写必填 name 参数。创建不覆盖同名对象，删除不额外调用其他对象的删除接口；服务端可因被引用等业务规则拒绝。

## 回读与结果含义

每次写入只发送一次，不自动重试。创建先查同名不存在；更新/删除核对 ID、原名及冻结状态。冻结状态未知时不写；已经符合明确请求的更新只读返回 not_needed。

当前部署单独 `$expand` 只返回导航字段，会省略 Name、InstanceID 和 IsFrozen。已在现场只读核实 `$select=*` 联合 `$expand` 可同时保留身份、普通字段及导航。自动前检/回读、详情展开和未指定 select 的列表展开均使用这个组合。

- verified=true：收到明确的 HTTP 成功响应，并且请求字段可在回读中核对一致；not_needed 表示原来已经一致，未写入。
- complete=false：可能仍有未核实字段，或回读发现其他非审计字段变化；应查看 field_checks、warnings 和 other_changed_fields，不能宣称全部配置保持不变。
- field_checks=null：Expanded 动作或服务控制字段并非明确的持久目标快照；会提交合法结构并返回可读证据，但不伪造动作解析或以回显证明持久化成功。
- submission_state=unknown/rejected：不会因回读同值而认定本次请求成功，不重发写入。超时后查到新对象或删除后404也不能单独证明未知请求的因果关系。

本轮没有为验收而执行实际创建、更新或删除。普通字段、引用和复杂明细的现场保存仍需按各自业务情形验收；本工具不会将一次 AllowMove/Spec 更新的经验泛称为所有字段已完成现场验证。

## 证据与测试

实时只读获取 `/swagger/ContainerLevels/swagger.json`，已归档为 `Swagger/modeling-containerlevels-swagger.json`，按 UTF-8/LF 规范化文本计算的 SHA256 为 `d12b6a04b28b92d315ad50dc04438ac1ddb6578d5c3f344028cef1c8510b2a61`。只读核实了 BOX 的身份、AllowMove、IsFrozen 格式及完整导航展开，没有发送业务写入。

`tests/test_container_level_update.py` 和 `tests/test_container_level_modeling.py` 覆盖原调用兼容、全部30个输入字段的类型路径、只读/选择器拒绝、多字段和引用/父子列表回读、显式清除及重命名、创建查重、单次删除、请求未知后的正确报告、选择值、MCP schema/分发/目录及读写路由。测试使用 MockTransport/AsyncMock 和网络拦截插件，不连接真实 MES 执行写入。

本轮最终回归：195 项测试通过，包含新建模工具、原 AllowMove 调用兼容、真实 MCP 注册/传输、字段完整覆盖及既有移动/审批/路由回归。全部写入均为离线模拟，没有进行真实业务保存验收。
