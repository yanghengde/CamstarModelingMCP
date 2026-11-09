# 第2部分：标准 BOM 维护

2026-11-09：本次约定的标准 BOM 维护已实现并完成独立现场验收。

| 功能 | 入口及结果 |
| --- | --- |
| 新建 BOM | `prepare_bom_change(action="create")`，明确名称和版本，支持描述、备注、ECO、状态及初始明细 |
| 明细维护 | `changes.lines` 按唯一 `name` 增加、修改、删除；`new_name` 重命名，未修改行和字段保留 |
| 用料设置 | 固定版本产品、主/副数量与单位、发料控制、装配顺序、位号、损耗率、备料量、允许超领/欠领等 |
| 升版 | `action="revise"`，明确当前 ROR 的 `source_revision`；显式复制源配置及明细，新版本成为 ROR，旧版保留 |
| 产品关联 | `prepare_product_bom_binding` 关联、换绑、解绑明确产品版本，固定 BOM 版本，保留其他产品配置 |
| 核实和追溯 | `execute_bom_change` 单次提交，`reconcile_bom_change` 回读核实；`get_bom_change_plan` 查看计划、原响应与审计 |
| 查询和比较 | `get_bom_details`、`get_product_material_requirements`、`compare_bom_versions`；明细按名称比较，引用按名称/版本/ROR比较 |

## 使用方式

先准备计划，核对 `before`、`expected` 与 `request_preview`，再执行。执行入口接收原计划的 `plan_id` 和 `plan_digest`；提交后调用核实入口。计划有效期一小时，已使用计划不可重放。

计划中的明细显示默认每页20条；`pages.complete=false` 时用 `get_bom_change_plan(offset,limit)` 继续查看。分页只影响显示，完整请求和摘要保存在本地，执行时不会裁剪明细。

```json
{
  "action": "create",
  "name": "DEMO-BOM",
  "revision": "1",
  "changes": {
    "description": "示例物料清单",
    "lines": [{
      "name": "L001",
      "product": {"name": "明确存在的物料产品", "revision": "1"},
      "quantity": "2.5",
      "unit": "EACH",
      "issue_control": 5
    }]
  }
}
```

新增行须明确产品版本、正用量、单位（允许明确 null）和发料控制。修改只传需要变更的字段；删除行传 `{"op":"remove","name":"L001"}`，不传其他字段。不推断单位、不自动换算、不把单位用量当作工单总需求。

发料控制建模值：1 序列容器、2 批量容器、3 外部批次、4 库位、5 仅数量、6 只显示不记录。配置这些值不代表对应实际投料事务均已接入。本轮对 5、6 完成现场保存和回读验证。

根字段：`description / notes / eco / status(1=Active,2=Inactive)`。

明细字段：`name / op / new_name / product{name,revision} / quantity / quantity2 / unit / unit2 / issue_control / spec{name,revision} / description / reference_designator / assembly_sequence / scrap_percent / setup_quantity / setup_quantity2 / allow_over_consumption / allow_under_consumption / allow_substitution / allow_all_revisions_as_substitute / vendor_control`。非新增字段允许省略；`unit / unit2 / spec` 可明确 null 清除。

## 现场验收

保留的测试前缀为 `CDX-BOM-20261109`，未修改既有生产对象。

- 三个新产品：`COMP-A / COMP-B / ASSEMBLY`。
- 最终 BOM：`CDX-BOM-20261109-FINAL`，保留版本 1、2、3，版本 3 为 ROR；测试装配产品固定关联版本 3。
- 创建两行；修改 A 用量、删除 B、新增 C；升版时修改用量且核实旧版；关联、换绑、解绑、重新关联全部通过。
- 将 C 重命名 D，明确修改产品、主/副单位与数量、位号、装配顺序、损耗率、备料量、发料控制及布尔设置，再从版本 2 复制版本 3；旧版保留，新旧明细比较无差异。
- 原生 HTTP 成功、请求字段与未修改配置回读一致，以及升版源版本保留均参与核实，不单凭状态码宣告成功。
- 探索阶段的 `BOM` 版本 1、`REVISION-CONTRACT` 版本 1/2 保留作接口证据。两个未通过核实的探索计划仍为 unknown，并继续占用其专属测试名称，未手工伪造成功或解锁。

关键现场契约：集合 PATCH 只创建空版本，不能将旧版行的 `materialList-Expanded` 修改直接作为复制升版。工具已改为在一次 PATCH 中显式发送新版本完整明细和支持复制的配置。源存在未支持复制的非空配置时，准备阶段阻断，避免丢失配置。

原始本地证据：`tmp/bom-acceptance/journal.jsonl`、`data/bom_modeling_plans.sqlite3`。另提供 [独立验收脚本](../scripts/bom_acceptance.py)，先生成唯一测试前缀的清单，再按清单执行；每次请求前落日志，异常停止、不自动重试、不删除证据。

相关自动化回归96项通过（BOM维护/读取、目录与路由、确认摘要、采集维护兼容）。MCP进程内列举与调用通过，当前源码注册317个工具，新增6个。全量测试尝试在历史测试中发现缺失的 `docs/t05-fixtures/CDX-T05-20261025-F.json`；单独停止于首个错误时为86项通过、1项资料缺失错误，未宣称全量测试通过。

## 边界与异常处理

每个 BOM 最多 200 行。ResourceBOM、幻影 BOM、替代料表维护、整 BOM 删除，以及批量迁移已有工单/容器物料清单不属于本次标准 BOM 维护范围。替代料允许标志可维护，替代料表另行处理。

冻结、锁定、版本不存在、重名、无效数量/枚举、未知字段、分页不完整和计划过期会阻断。执行前重读目标及引用对象，发现变化则不提交；这属于客户端变更检查，不能替代 MES 对外部并发写入的事务控制。

超时、响应丢失或回读不匹配保留 unknown，同名 BOM/相关产品不能通过另建计划重复提交。`review_bom_change` 仅用于有最终人工证据的审计；崩溃恢复保留占用，原计划永不重放。

代码已接入本地 Agent 目录、分组路由、确认摘要及外部 MCP 初始化说明。运行中的服务若未自动重载，需重新启动后才会出现新增工具；不自动终止用户在 VS Code 启动的进程。
