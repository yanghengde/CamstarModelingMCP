# 数据采集 MCP 工具链

2026-10-25：collection组共20个工具，覆盖定义/参数/组维护、工序关联、值校验、预览、单次提交、历史查询及核实/恢复。新增工具已注册到MCP、Agent分发、能力目录和确认策略。注册和模拟回归不等于现场写入验收。

## 工具入口

| 环节 | 工具 | 行为 |
|---|---|---|
| 定义查询 | `list_data_collections`、`get_data_collection_details` | 普通user/分组point，固定版本或明确ROR；分页与不完整标记 |
| 定义和参数维护 | `prepare_data_collection_definition` | create/update/revise/delete；普通点、组和组内点的新增、修改、移除 |
| 工序关联 | `prepare_data_collection_binding` | 明确Spec版本和定义版本，绑定/修改/解绑CollectData映射 |
| 配置计划 | `get_data_collection_change_plan`、`execute_data_collection_change` | 不可变1小时计划；确认、重读、摘要比较、单次执行 |
| 配置核实 | `reconcile_data_collection_change`、`review_data_collection_change` | 原响应与回读核实；人工最终证据审计和本机中断恢复 |
| 容器要求 | `get_container_collection_requirements` | 当前Spec → TxnMap → 明确定义；动态要求仍单列未知 |
| 值校验 | `validate_collection_values`、`validate_grouped_collection_values` | 类型、必填、常量限值或样本数；无质量结论 |
| 采集提交 | `preview_container_data_collection`、`get_collection_submission_plan`、`collect_container_data` | execute=false回显、确认、提交前重读、单次领取 |
| 历史 | `get_container_collection_history`、`get_grouped_collection_history` | 普通点历史、分组样本历史；分页不代表完整一致快照 |
| 提交核实 | `reconcile_collection_submission`、`verify_collection_submission` | 前者只对照用户给定候选事务；后者核实新计划唯一标记及完整历史链并保存本地终态 |
| 提交恢复 | `recover_collection_submission_attempt`、`resolve_collection_submission_outcome` | 保留原请求和结果，人工最终证据核实；原计划不可重开 |

所有实际写入和改变占用状态的核实/审计工具均always_confirm=true。准备计划只读MES并写本地计划库，不授予执行权限；直接MCP调用者也须落实同等确认。

## 定义维护示例

```json
{
  "kind": "user", "action": "create", "name": "InspectionData", "revision": "1",
  "changes": {
    "description": "检验参数", "layout": 1,
    "points": [
      {"name": "Temperature", "data_type": "decimal", "required": true,
       "lower_limit": "10", "upper_limit": "30"},
      {"name": "Accepted", "data_type": "boolean"},
      {"name": "MeasuredAt", "data_type": "timestamp"}
    ]
  }
}
```

上例传给prepare_data_collection_definition，审阅返回的before/expected/request_preview，确认后把原plan_id和plan_digest交给execute_data_collection_change。

根字段：description、layout、display_limits、status；分组定义使用instructions，不接受普通定义的layout/display_limits。

点字段包括name、data_type、required、row_position、column_position、decimal_scale、rounding_rule；普通值点还支持unit（明确UOM名称或null）、lower_limit、upper_limit、display_limits、limit_override_allowed、boolean_true/boolean_false、attribute_name、map_to_user_attribute。限值表达式原样保存，不在客户端求值。对象点使用ObjectDataPointChanges，必须明确object_type的CDO编号，可配置object_selection_type、display_mode、list_field_expression、query_name、query_type、validate_resource；对象组/查询参数的通用维护暂未开放。

普通值点使用#modeling.ValueDataPointChanges；基础DataPointChanges没有单位和限值，不能把这类字段写到基础类型。

update的points变更每项明确op=add/update/remove，默认update，以name定位；new_name可重命名。修改采用dataPoints-Expanded，未修改点和属性保留；remove使用明确键与空value。拒绝重名、未知/只读字段以及对象点与值点的原地类型切换。revise明确source_revision，须当前ROR且新版本尚不存在，使用服务PATCH升版。delete只针对明确版本，拒绝冻结/状态未知对象，由MES继续校验引用约束；本轮未执行任何删除。

分组示例：

```json
{
  "kind": "point", "action": "create", "name": "Dimensions", "revision": "1",
  "changes": {"instructions": "每点采两次", "groups": [
    {"name": "Size", "minimum_samples": 2, "layout": 1,
     "points": [{"name": "Thickness", "data_type": "decimal", "minimum_samples": 2,
                 "outer_lower_limit": "1", "outer_upper_limit": "5"}]}
  ]}
}
```

groups及groups[].points同样支持op/new_name；组支持layout/minimum_samples/sequence。组内点使用DataPointCollectionPointChgs，支持integer/string/timestamp/boolean/decimal、unit、minimum_samples/sequence及inner_lower_limit/inner_upper_limit/outer_lower_limit/outer_upper_limit。

关联使用prepare_data_collection_binding(spec_name, spec_revision, kind, definition_name, definition_revision, action, required, sequence)。仅CollectData，实时元数据中的服务CDO编号为2200；继承ContainerTxn字段的1110不能代用。保留其他TxnMap项，无名映射按已核实索引更新；重复映射、同名定义其他版本冲突会拒绝。

## 采集输入与提交

preview_container_data_collection的collection_kind=user时values为点值映射；collection_kind=point时为组/点/样本映射：

```json
{"Size": {"Thickness": [2.1, 2.2]}}
```

普通点支持integer/float/fixed/decimal/string/boolean/timestamp；分组支持integer/decimal/string/boolean/timestamp。布尔必须JSON布尔，整数必须32位，数值拒绝NaN/无穷大和数字字符串；时间必须带明确时区，如2026-10-25T10:30:00+08:00，比较保留.NET时间的第7位小数。对象引用的实际采集提交暂阻断，不把对象字典转换成字符串。

分组当前要求明确提供全部组、全部点，组内等样本数，满足组与点最少采样数；最多50组、每组200点、每点200样本、总计1000样本及100000字符。暂不支持部分组/部分点、任意网格CellName和自定义ParametricData。

预览读取明确定义、容器及CollectData工序映射，分别构造DataPointSummary或DPCollection，并核对execute=false的容器/定义版本、父链、组/点/样本ID、顺序和值回显。只有全部核对通过才生成计划；executable不是质量合格或已提交结论。新增计划的Comments包含随机唯一MCP-COL标记，重读和执行复用同一标记。

提交再次构造预览并比较不可变摘要；只发一次执行请求，取消/超时也先持久化unknown，不自动重试。普通/分组共用计划库和容器占用。配置维护与采集提交使用共同进程锁；相关定义/工序有未知维护或相关容器有未知提交时，拒绝绕过保护。核实或人工最终证据审计只解除该计划占用，原计划永久不可重用。

verify_collection_submission先核对唯一Comments标记的唯一HistoryMainline记录、CollectData类型2200、容器和Spec，再比对全部点/样本、定义ID、数据类型、值和事务父链。只有完整匹配才在同一SQLite事务保存证据、原响应与终态；空页、重复、多主线、截断或同值其他事务均不算成功。旧版无标记计划仍通过人工最终证据审计，不追认来源。

普通历史链：DataPointHistoryDetail → DataPointHistory → HistoryMainline。分组历史链：DPCollectionSampleHistory → DPCollectionPointHistory → DPCollectionGroupHistory → CollectDataPointsHistory → HistoryMainline；注意数据库的样本父字段实际拼写为DPCollectionPointsHistoryId。

## 验证、限制和部署

本轮仅读取现场Swagger/EDM、数据库列元数据并运行只读计划准备和空历史查询；没有创建/修改MES定义、工序或容器，没有执行CollectData。证据在[只读预检](collection-fixtures/read-only-preflight.json)。配置写入、布尔/时间/分组实际执行、非空历史关联及客户端联合验收继续进入集中验收清单；不能把注册、模拟测试或空历史查询称作现场验收完成。

动态枚举/对象选择值、单位换算、表达式求值、覆盖越限、电子签名和定制事务没有通用已验证入口。工具保留未知项，不自动关闭MES校验，不推断合格/放行。

新增配置库data/collection_modeling_plans.sqlite3；采集沿用data/collection_plans.sqlite3。核实/恢复依赖同机SQLite和完整锁文件，所有写入进程必须统一升级。代码合入后需重启MCP/Agent服务并刷新客户端工具清单，本轮未重启运行服务。

本轮最终全量1176项测试及2项子测试通过，离线HTTPX真实网络尝试0，git diff --check通过。较1131项基线新增45项测试。另修复既有故障测试在Windows读控制文件期间替换文件的偶发竞争，仅重试本地测试文件交换，业务请求仍不重试。证据：[JUnit](collection-fixtures/final-junit.xml)、[日志](collection-fixtures/final-test-output.txt)、[网络报告](collection-fixtures/final-network.json)。

2026-10-25多轮评测续接：changes已在MCP schema显式描述点/组嵌套类型，限值必须是字符串或null；语义与原运行时校验一致。审批中的新指令会撤销旧调用后重新处理，避免旧参数继续执行；真实模型评测及仍存在的重复确认问题见[多轮评测报告](multiturn-evaluation/README.md)。
