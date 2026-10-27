# AQL等级与检验水平配置

2026-10-26：新增3个质量工具，quality分组共39个工具。范围为配置读取、维护计划和只读查表预览；尚未进行真实配置写入与检验业务闭环验收。

| 工具 | 关键参数 | 返回内容 |
| --- | --- | --- |
| `get_sampling_reference_definition` | `reference_kind`、`name`、可选`instance_id` | AQL或检验水平完整表、冻结标志、明细ID及变更计数 |
| `prepare_sampling_reference_definition` | `reference_kind`、`action`、`name`、`changes`、可选`instance_id` | 不写MES的静态维护计划，供统一执行工具确认后单次提交 |
| `preview_sampling_table_lookup` | `inspection_level_name`、`aql_level_name`、`lot_quantity` | 批量区间、原样代码、配置样本量、接收/拒收数及缺项提示 |

`reference_kind`仅为`aql_level`或`inspection_level`。这两个对象分别通过Modeling的`AQLLevels`、`InspectionLevels`管理，属于命名对象，没有Revision或Status字段。动作只支持create/update，不提供升版、对象重命名或整表删除。名称与代码受现场nvarchar(30)限制，说明受nvarchar(255)限制；同时校验UTF-16存储容量。

## 配置与明细维护

AQL的`SampleSizeDetails`保存`SampleSizeCode`、`SampleSize`、`Accepts`和`Rejects`。`Accepts`为仍可接收的拒收数量上限；拒收数量达到`Rejects`时拒收。工具分别保留这两个值，不将其中一个默算为另一个。当前维护子集要求：

- 用户明确正整数`sample_size`，传给Camstar时保持其字符串字段类型。
- `0 <= accepts <= sample_size`，并且`accepts < rejects <= sample_size + 1`。
- 允许两个阈值之间存在空档，查表预览不对空档作质量判定。
- 同一AQL表内的代码不得重复；大小写不同的同名代码也拒绝，避免歧义。
- 可以只读原样查看已有公式、百分比或箭头规则；当前不求值，也不维护含这些规则的有效表。

检验水平的`LotSizeDetails`保存`MinLotQty`、`MaxLotQty`和`SampleSizeCode`。本工具按闭区间校验，禁止重叠或共用端点；允许显式空档，预览查不到区间时报错，不填补空档。一个代码可出现在多个互不重叠区间。

每表最多50行，读取时必须完整；明细缺失、分页、重复ID、错误父级、未知冻结状态或读取期间变化都会失败。维护计划逐行使用`op=add/update/remove`：

- 新增行须提供全部业务字段，不接受既有明细ID。
- 更新或删除须指定实际`detail_instance_id`，同一行不能重复维护。
- 更新只提交明确字段；删除不能夹带其他修改，不能删除最后一行。
- 不维护冻结对象或被冻结的目标行。
- 更新先于删除执行，删除按原索引倒序，新增在最后，避免索引偏移。

执行预览包含完整有效表、实际请求体、原状态及`codes_removed_from_table`。本轮未验证所有其他表对代码的引用，`cross_table_references_verified=false`；改代码或删行前应核对关联检验水平与AQL表，不自动级联改表或回填已有任务。

## 示例

以下是用户明确给定的一组演示值，不代表任何抽样标准表。创建AQL配置：

```json
{
  "reference_kind": "aql_level",
  "action": "create",
  "name": "DemoAQL",
  "changes": {
    "description": "用户指定的演示配置",
    "aql_rows": [
      {"op": "add", "sample_size_code": "C", "sample_size": 20, "accepts": 1, "rejects": 2}
    ]
  }
}
```

创建检验水平的参数：

```json
{
  "reference_kind": "inspection_level",
  "action": "create",
  "name": "DemoLevel",
  "changes": {
    "lot_ranges": [
      {"op": "add", "sample_size_code": "C", "min_lot_qty": 100, "max_lot_qty": 500}
    ]
  }
}
```

先分别审阅准备结果，再由`execute_quality_transaction(plan_id, plan_digest)`确认、单次提交。Modeling维护没有execute=false的非写入回显；`static_and_echo_verified=false`、`dynamic_validation_verified=false`，预览不能证明MES业务规则已通过。执行后仍为unknown，不自动重试；同值当前表也不能证明是本次维护成功，仍通过原有最终证据审计流程核实。

为避免未核实配置维护与采样交叉，尚有unknown/in_flight的抽样基础配置维护时，其他质量采样、定义维护、产品抽样绑定和建批保持阻塞；反向亦然。现有任务快照未总是公开完整的AQL/检验水平依赖链，当前采取保守占用；NCR/CAPA不因这项配置占用而被统一阻塞。

提交前会重新读取完整表并重建计划摘要，检测已读取到的根配置和明细变化；这不提供服务端条件写入或数据库原子快照保证，外部客户端在最终读取与PUT之间的并发修改仍需现场验证。

## 查表预览

```json
{
  "inspection_level_name": "DemoLevel",
  "aql_level_name": "DemoAQL",
  "lot_quantity": 120
}
```

`preview_sampling_table_lookup`先找唯一闭区间，再用原样代码精确匹配AQL行。它返回配置样本量20及原接收/拒收数，不改MES。所有表均完整读取并重新核对变化；冻结行、重复代码、缺少匹配、公式等拒绝预览。返回`unmapped_inspection_codes`用于发现其他区间在AQL表中的缺项。

若配置样本量大于批量，返回原样配置并设置`sample_size_exceeds_lot=true`，不自动裁剪。不应用MES的任务冻结、频率、切换规则或批量调整；`mes_task_requirements_verified=false`、`standard_compliance_verified=false`，`quality_verdict=null`。真实采样要求仍以`get_container_inspection_tasks`或抽样批任务为准。

## 多轮问法

| 用户问法 | 工具顺序 | 关键处理 |
| --- | --- | --- |
| “看看AQL表” → “把这一行样本量改成20” | 读取完整表 → 明确所选行ID → 准备update → 确认执行 | 保留其他行，不默认替换整表 |
| “检验水平1到10用A，11到100用B” | 准备检验水平create/update | 需要名称和明确动作；区间不允许共用端点 |
| “按批量10看样本量” → “如果是11呢” | 保持明确的两张表名称，分别调用查表预览 | 对10、11分别匹配；不把配置预览说成MES任务结论 |
| “删掉A代码” | 读取表并取得明细ID → 准备remove | 展示删除代码及有效表；最后一行不能删除，关联表须核对 |
| “提交超时了，再试一下” | 检查原计划及最终审计证据 | 不重放或换计划绕过unknown占用 |

## 验证证据

本轮新增69项针对性测试，全量1641项测试、2项子测试通过，质量相关435项；离线父pytest进程的真实HTTPX网络尝试为0。真实FastMCP Client完成两类配置的模拟准备/提交、unknown占用、人工证据审计与查表流程；模拟结果不代表现场业务验收。

- [Swagger、EDM与物理列](quality-fixtures/sampling-reference-contracts.json)
- [现场只读探测及字符容量](quality-fixtures/sampling-reference-read-validation.json)
- [本轮回归摘要](quality-fixtures/sampling-reference-summary.json)

现场AQL和检验水平列表均为空；合法GET/select/expand及ChangeCount SQL已验证，无非空目标、真实写入或MES任务闭环验收。服务未手动重启。
