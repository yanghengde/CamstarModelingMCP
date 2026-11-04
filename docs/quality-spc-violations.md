# SPC明确报告结果登记：受限首版

C6 按现场 EDM 修正了违例预览查询，并增加精确标记与请求内容匹配的历史候选。匹配候选用于人工审计。详见[C6 现场与写入证据](quality-spc-history.md#现场与写入证据)。

2026-10-29。新增 `get_spc_violation_context`、`prepare_spc_violation`，该阶段quality分组55个工具；后续新增[基础Event首次分流](quality-event-triage.md)，当前68个工具。在原始历史点注释之外，提供原生 `RecordSPCViolation` 的单点结果登记计划。

**这是登记调用方明确报告的结果，不是工具计算或认证现场SPC引擎。** 报告的控制限、绘图值及来源真实性未被自动核实；本地WECO信号不能被模型自动填成现场引擎结论。准备工具只保存本地计划；实际提交复用系统确认后的 `execute_quality_transaction` 单次执行。现场元数据读取未成功，本轮没有MES业务写入或服务重启，真实非空数据、预览、提交和历史效果待验收。

## 工具与参数

| 工具 | 用途 |
|---|---|
| get_spc_violation_context | 原始直接数值采集点、容器/固定定义/SPC组、当前完整规则成员和最多100条原点有效违例历史；可明确选择violation_name核对归属 |
| prepare_spc_violation | 明确报告来源、原值/绘图值/单位及控制限后，准备一个原点、一个图、一个明确违例的原生嵌套登记计划 |

定位参数沿用[SPC注释](quality-spc-annotations.md)：`container_name`、真实 `data_point_history_id`（DataPointHistoryDetailId）、`spc_chart_name`，以及可选的原容器/原图持久ID。定义点ID、本地observation_id、样本ID或外部数据ID都不能代替历史点ID。

查询时不传 `violation_name` 会列出完整规则成员的ID、名称和冻结状态，不自动挑选。准备时必须明确 `violation_name`，它须属于所选图当前规则的完整有效清单。

`reported_result`是严格封闭对象，无默认值，以下全部必填：

| 字段 | 含义与校验 |
|---|---|
| source_kind | 固定operator_reported，表示本工具接收的是调用方报告；不宣称本工具已验证报告由哪个引擎生成 |
| evidence_reference | 调用方明确提供的报告编号/归档位置，1..512字符；只保存引用，不自动访问路径/URL，不证明文件存在或内容真实 |
| reported_value | 报告的采集原值，十进制字符串，须与原始持久历史数值精确相同 |
| plotted_value | 报告的绘图值，必须明确给出；首版只允许与原值数值相同，不自行计算子组均值、极差、变换或其他派生值 |
| uom_id | 报告中原值、绘图值和控制限共用的单位ID，须与原始历史UOMId一致；原单位未知则拒绝，不换算 |
| lcl / center / ucl | 明确报告的控制下限、中心线、上限；LCL<中心<UCL，不接受规格限字段，不用待评估点反推控制限 |

数字字段为十进制字符串，绝对值≤1e12，最多12位小数，不接受指数、NaN、无穷、布尔和JSON数值。使用精确数值比较，原值必须严格低于LCL或高于UCL；等于边界、区内运行规则、子组/变换结果不支持。限值须能经本接口Double的十进制字符串往返；不默默改变报告数值。

下面仅为模拟报告结构，实际原始点、单位ID、控制限和来源必须使用调用方真实明确提供的内容：

```json
{
  "source_kind": "operator_reported",
  "evidence_reference": "SPC operator report 2026-10-29/17",
  "reported_value": "10.1",
  "plotted_value": "10.1",
  "uom_id": "00000000000000d2",
  "lcl": "0",
  "center": "5",
  "ucl": "10"
}
```

数据一致及单点越限检查不证明报告、限值、规则参数或图类型语义真实有效。编译规则文件、现场引擎版本/参数、控制限来源内容、统计假设和具体图类型的适用性均未自动认证；不能据此批准质量、放行或声称已完成现场SPC计算闭环。

## 准备、执行与核对

1. 取得真实原点ID和图，先查询上下文与当前规则候选。原始点→父历史→原采集事务、原容器、固定定义版本和完整SPC组的核对沿用注释入口。
2. 调用方明确报告结果、来源引用、单位/数值/控制限及违例名称，才调用 `prepare_spc_violation`。来源缺失时不由模型补造；不能将本地分析、规格限或模型猜测转换为该报告。
3. 审阅原点、原容器、图、当前规则名称和明确违例，特别查看 `reported_result`。预览明确返回external_engine_result_verified、reported_limits_verified_against_engine、evidence_reference_verified=false。
4. 系统确认后，由现有执行器重建快照、核对摘要与读写占用，单次POST原生服务，不重试、不跟随重定向。每个请求仅一个图明细与一个点明细，不循环单点模拟批量事务。
5. 提交后先unknown，保留原点与容器占用。`inspect_quality_transaction`仅给原点/原图/原规则的当前证据，不因HTTP200、同值或已有历史自动成功/解锁。最终事务证据由 `review_quality_transaction` 人工审核；不得换图、改名、另建计划或切换注释工具绕过未知占用。

结果来源引用保存在本地不可变计划和确认预览中；MES Comments保留唯一关联标记及 `operator-reported SPC result` 标签，不能将它描述为经本工具认证的引擎结论。原始采集值不修改。已有同原点/图/违例的有效记录则拒绝重复登记，不提供覆盖或更正历史功能。

## 配置与副作用边界

- 原点必须是未冲销、Active普通固定采集定义中的直接数值历史点，原图属于完整SPC组、非自定义数据源；单位已明确。图变量须与原始点名完全相同，不推测SQL列或表达式映射。
- 当前规则通过图的持久引用定位；读取规则完整成员及名称/冻结状态，所选违例须在清单中且未冻结。绑定规则、成员和违例定义的ChangeCount及内容；不把名称匹配说成编译规则或参数等价验证。
- 规则清单使用LEFT JOIN保留悬空引用，缺失、重复、未知冻结状态或超过100项均拒绝，不能静默丢弃成员后称完整。
- 任意图对原点存在有效排除记录时拒绝；不排除、不恢复数据。
- 图配置EmailNotification、EMailMessage、PutContainerOnHold、SetResourceStatus非空或字段未知，以及TriggerOCAPs=true/未知时拒绝。不会为了登记而清空或关闭这些配置。
- 原生图明细EMailMessage、Recipients、Message有从图派生的逻辑，不能因请求未填邮件就认为不会发送。因此非执行预览必须明确返回空通知/文件默认，点明细的HoldReason、Resource、ResourceStatusCode须明确为空；未知、缺失或派生非空均拒绝。
- 不接受邮件地址、文件路径、冻结/资源动作、外部源、签名开关等透传参数。签名要求缺失、未知或非空时拒绝此首版，不收集或存储口令、不覆盖签名校验。无签名要求不代表权限或业务批准已通过。
- 准备期间和执行前重读原点、图、规则/清单、定义、注释与违例历史；变化则拒绝提交。检查unknown时固定原始持久ID，原父关联或原规则被替换则拒绝跟随。重读与本地占用不提供跨系统原子快照，不协调其他独立事务库，也不证明部署扩展无副作用。

## 原生请求与历史依据

请求结构为 `RecordSPCViolation.ServiceDetails[0] → RecordSPCViolationDetail.Violations[0] → SPCViolationDetail`。预览核实原生类型8400/8401、原历史点及父引用、原容器、采集原值、明确报告的绘图值与控制限、当前规则名称和明确违例名称。FailureAction按标准原生逻辑为Violation；不附加自动处置。

历史并非单表，也不能按名称猜主键：

| 原生表 | 持久主键 | 父引用 |
|---|---|---|
| RecordSPCViolationHistory | RecordSPCViolationSummaryId | HistoryMainlineId |
| SPCChartHistoryDetail | RecordSPCChartViolationHistDId | RecordSPCViolationSummaryId |
| SPCViolationHistoryDetail | SPCChartViolationsHistDtlsId | ParentId指向图明细 |

按这三层真实父键连接HistoryMainline，并核对所有TxnId一致、点明细HistoryId与主线HistoryId一致、原点/容器、TxnType=8396和ReversalStatus=1。不假造汇总/图明细中的实体HistoryId列。最多100条，重复、悬空链或达到101条不当作完整历史；空结果不证明提交失败。

该标准快照中的SPCRules映射为A_SPCRules；SPCViolation表的持久键叫SPCRuleViolationId，清单外键名则为SPCViolationId。固定SQL字段已逐项与官方元数据列目录核对；运行时仍必须通过当前dbo字段检查，否则拒绝准备。

依据：[现场Swagger归档](quality-fixtures/RecordSPCViolation-schema.json)、[原生模型](quality-fixtures/spc-violation-native-model.json)、[历史/规则目录](quality-fixtures/spc-violation-native-history.json)、[历史父关联](quality-fixtures/spc-violation-native-links.json)、[规则清单](quality-fixtures/spc-violation-native-rules.json)、[补充列目录](quality-fixtures/spc-violation-native-columns-extra.json)、[原生执行CLF](quality-fixtures/spc-native-execution.json)。标准MDB源SHA256为`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不证明部署自定义一致。

## 验证与剩余开发

[本轮现场只读检查](quality-fixtures/spc-violation-live-readcheck.json)未完成元数据读取，不能据此判断表为空或当前部署字段已核实。本轮未向MES写入业务数据，没有创建图/规则或业务样本，也未重启服务。

本轮新增101项测试；专项351项通过，最终全量2652项及2项子测试全部通过，其中质量专项1446项通过。最终离线测试进程的真实HTTPX传输尝试为0。

新增测试覆盖报告来源/原值/绘图值/单位、精确越限、规则完整性和冻结状态、真实主键及父链、通知/自动处置阻断、非执行默认、签名未知、配置变化、重复及跨工具占用、超时/取消/HTTP400后的unknown、原ID检查、MCP参数封闭和系统确认。见 [测试汇总](quality-fixtures/spc-violation-summary.json)、[全量JUnit](quality-fixtures/spc-violation-junit.xml)、[最终离线HTTPX尝试记录](quality-fixtures/spc-violation-network.json)、[工具和问法核对](quality-fixtures/spc-violation-tool-manifest.json)。离线守卫不是操作系统级网络隔离，问法检查只验证确定性工具选择。

仍未完成：自动读取并认证现场SPC引擎/编译规则与控制限、区内运行规则和子组/变换结果登记、批量事务、自动最终历史关联，以及真实非空配置/预览/提交的验收。复杂质量流程、批报废、审批和电子签名仍按[高级功能计划](quality-advanced-roadmap.md)继续推进。
