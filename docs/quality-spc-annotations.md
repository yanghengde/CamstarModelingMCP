# SPC原始历史点注释：受限首版

2026-10-29。在[SPC本地计算与判异](quality-spc-analysis.md)之外，新增两个MCP工具，该阶段quality分组为53个工具；后续新增[明确报告的单点SPC违例登记](quality-spc-violations.md)，SPC扩展阶段55个工具；当前65个工具，另有[事件文字资料维护](quality-event-update.md)，另有[基础Event首次分流](quality-event-triage.md)：

| 工具 | 功能 |
|---|---|
| get_spc_annotation_context | 核对原始采集点、容器归属、父历史/事务链、固定采集定义版本、完整SPC组成员，读取该点所有控制图的有效注释历史 |
| prepare_spc_annotation | 准备原生AddSPCAnnotation计划，非执行预览后保存本地计划，不提交MES |

复用 `execute_quality_transaction` 确认单次提交、`inspect_quality_transaction` 只读证据及 `review_quality_transaction` 最终证据人工审核。没有新增直接写入口。本轮现场Query连接失败，没有验证非空实际数据、非执行回显或业务提交；代码和模拟测试通过不等于现场验收。

## 参数及调用顺序

两个新工具共用下列定位参数：

| 参数 | 要求 |
|---|---|
| container_name | 原始采集历史所属容器名称，不代开工、不移站 |
| data_point_history_id | 真实16位十六进制 **DataPointHistoryDetailId**，不是DataPoint定义ID、样本ID、本地分析observation_id、事务ID或外部数据ID |
| spc_chart_name | 明确的SPC图名称，必须属于原始采集定义当前完整SPC组 |
| container_instance_id | 可选；已知时用原容器持久ID固定身份 |
| spc_chart_instance_id | 可选；已知时同时核对图名称与持久ID |

`prepare_spc_annotation` 还必填 `annotation`：1..2000字符非空文本，不自动改写或缩短，不接受NUL。没有exclude、自定义数据源、规则判定、邮件、冻结、资源状态、签名开关或原始值覆盖参数。

1. 通过已有采集历史查询取得原始DataPointHistoryDetailId，明确容器和图。缺少真实ID时先查询，不从本地SPC编号构造ID。
2. 调用 `get_spc_annotation_context`。返回完整上下文只代表本次有界读取完整，不证明允许提交或数据已进入现场SPC引擎。
3. 明确注释后调用 `prepare_spc_annotation`，审阅原点、容器、固定定义版本、图和原文；计划仅本地保存，1小时有效。
4. 通过现有系统确认调用 `execute_quality_transaction(plan_id, plan_digest)`，重建并比较快照后只发一次请求。请求不重试、不跟随HTTP重定向。
5. 提交后先保留unknown及占用，即使HTTP200或相同注释已可见也不自动成功。使用 `inspect_quality_transaction` 看原始点的当前证据，最终事务证据经人工 `review_quality_transaction` 记录终态。不得换图、改名、另建计划或清库绕过未知占用。

注释可如实写入人工调查说明；本地分析信号不能自动变成“MES已确认违例”的结论。`RecordSPCViolation`已有独立的明确报告结果受限登记计划，不能由本地分析自动触发，范围见后续文档。

## 校验范围

- 运行时查询当前dbo下的必要表字段；缺失、重复、错误部署或无法查询时拒绝准备，不把标准元数据快照当成现场字段检查通过。
- 读取原始点→DataPointHistory→HistoryMainline，核对父ID、固定定义ID、HistoryId、TxnId及容器；要求未冲销、普通直接数值采集，保留原始数据值和单位引用。原始值不被修改。
- 只支持Active且未冻结的UserDataCollectionDef固定版本。图必须未冻结，`RecordAgainstCustomSPCDataSrc=false`，且位于原定义SPCChartDefGroup完整ResolvedEntries中。缺失/重复成员、分页或超过100项时拒绝，不使用首条或近似匹配。
- 图和采集定义读取持久ChangeCount，并绑定配置内容；SPC组绑定完整内容和成员。非执行预览后重读全部原始点、容器归属、配置、组及注释历史；执行前再次重建，任何变化均拒绝提交。重读不提供跨MES请求的原子快照，也不证明部署扩展不存在。
- 原生注释中的DataPoint是 **历史明细** 引用，Parent为原DataPointHistory；预览必须完整回显原引用、图、注释、Exclude=false和关联标记。MES派生DataCollectionDef的ID、名称、版本也须与原记录一致。
- 明确读取ESigRequirement和AdditionalESigReqs；缺失、未知或非空时拒绝此受限计划，不关闭签名校验，不收集或持久保存签名口令。无签名要求也不是角色权限或业务批准证明。

## 排除数据与历史范围

标准 `GetSPCAnnotations` 查询虽然被传入SPCChartDef参数，但实际查询文本没有按图过滤。因此本工具读取同一原始点 **所有图** 的有效注释，不将当前图以外的排除记录隐藏。

请求中的 `Exclude` 固定false。任意图存在有效Exclude=true记录都拒绝准备，不尝试恢复、覆盖排除或按最新文本推测现有排除状态。排除和恢复数据不属于本轮开放范围。

历史读取使用 `AddSPCAnnotationHistory → HistoryMainline` 的持久父键，并核对HistoryId=原历史点ID、TxnId一致、TxnType=8393及ReversalStatus=1。最多100条，达到101条则报告不完整并拒绝；不截断后认定全部。空记录不证明提交失败，不允许重试。分组采样、自定义/外部SPC数据源、冲销记录及通用历史关联均未扩展。

同一质量事务库中，unknown/in_flight保留原始点与原容器占用，并声明图、采集定义、SPC组的配置读资源。检查unknown时固定原始点、原容器和原图的持久ID；原父记录/定义等关联变化则拒绝，不跟随同名替代图或新父级。不同图也不能绕过原点占用。其他系统/独立事务库不受此本地锁统一控制，不承诺跨服务互斥。

## 原生依据与本轮联调

- [原有AddSPCAnnotation现场Swagger归档](quality-fixtures/AddSPCAnnotation-schema.json)：原生注释字段、历史字段、引用形状。
- [官方Designer字段目录](quality-fixtures/spc-write-native-fields.json)、[完整字段及查询关联](quality-fixtures/spc-write-native-links.json)：DataPoint类型为DataPointHistoryDetail，DataCollectionDef由原点定义父级派生，Exclude默认0，HistorySummary为AddSPCAnnotationHistory。
- [原生注释选择查询](quality-fixtures/spc-write-native-history-query.json)：GetSPCAnnotations按原点HistoryId及有效冲销状态读取，未按图过滤。
- [原生表目录](quality-fixtures/spc-write-native-tables.json)、[原生列目录](quality-fixtures/spc-write-native-columns.json)：固定查询字段的标准来源。元数据源SHA256为`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，标准csi快照不证明部署自定义一致。
- [本轮现场元数据只读查询](quality-fixtures/spc-write-live-columns.json)：ConnectError，列查询未完成，不能宣称现场结构/非执行回显或业务效果已验收。本轮未发送业务写入、创建业务配置、触发通知或重启服务。

## 测试及剩余开发

测试覆盖准备/真实MCP调用与确认、原点及父链身份、字段完整性、配置与组成员、跨图排除保护、签名未知、原生回显字段、准备期间及执行前变化、字段/服务绕过、同点/容器占用、超时/取消/HTTP400后的unknown、防重放及只读inspect。还核对固定查询字段确实出现在官方标准列目录中。

本轮新增115项测试；专项250项通过，最终全量2551项及2项子测试全部通过，其中质量专项1345项通过。最终离线HTTPX真实传输尝试为0。

最终结果见 [本轮汇总](quality-fixtures/spc-annotations-summary.json)、[全量JUnit](quality-fixtures/spc-annotations-junit.xml)、[离线HTTPX尝试记录](quality-fixtures/spc-annotations-network.json)、[工具与参数清单](quality-fixtures/spc-annotations-tool-manifest.json)。离线测试不是操作系统级网络隔离；问法检查只验证确定性工具选择，不代表模型已经正确完成现场多轮业务。

仍需开发/验收：真实非空原点和图配置的联调、非执行预览与实际注释历史、部署扩展影响、自动最终事务关联、现场SPC引擎/编译规则认证及更广违例写入（明确报告的单点结果已有独立受限入口）。SPC引擎调用、排除/恢复、自动冻结、资源状态、通知和其他质量高级流程仍未完成。
