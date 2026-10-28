# NCR升级质量事件

2026-10-28新增两个MCP工具，质量分组共46个工具。

| 工具 | 用途 |
|---|---|
| `get_ncr_escalation_options` | 指定组织的完整分类/子分类组合、映射ID和电子签名配置，只读 |
| `prepare_ncr_escalation` | 对既有Open NCR准备正式NCREscalate升级请求，不写MES |

执行仍使用 `execute_quality_transaction`；检查使用 `inspect_quality_transaction`；最终证据审计使用 `review_quality_transaction`。不新增绕过统一确认的直接执行入口。

## 调用流程

1. 用 `get_nonconformance_report` 读取既有报告及失败代码。报告必须Open（状态1）、没有已升级事件关联，关闭时间为空；关联容器须Active。
2. 用 `get_ncr_escalation_options` 明确组织，检查其允许的分类/子分类组合。优先级用 `list_quality_references(kind="priority")` 查询，再由用户明确选择。不能从全局分类列表任意拼接组织未允许的组合。
3. 准备明确请求：

```json
{
  "report_name": "既有NCR名称",
  "organization_name": "明确组织",
  "classification_name": "该组织映射中的分类",
  "subclassification_name": "该组合的子分类",
  "priority_name": "明确优先级",
  "comments": "明确的升级说明"
}
```

4. 展示原报告、容器状态、组织组合和最终请求。确认同一 `plan_id` 与 `plan_digest` 后调用 `execute_quality_transaction`。只有一次请求，HTTP200也先保存为unknown。

准备阶段核对原NCR及完整失败代码、原容器和四类命名参考对象；组织映射完整有界最多100条。映射和参考配置的冻结表示配置不可修改，不据此断言NCR不能升级。已有冻结的Active容器可以准备升级，不会调用解除冻结。

## 分类映射必须唯一

部署组织对象的 `EventClassificationSpecMaps` 定义有效组合。子分类字段在该明细中是 **`Subclassification`**；NCREscalate请求根字段则是 **`subClassification`**，两者按正式契约分别处理。

所有映射必须有有效且唯一的持久化ID及分类/子分类引用。父级返回时检查其组织归属，检测分页或超过100条时拒绝将子集当成完整表。准备阶段要求选择的分类和子分类只匹配一条组织映射；零条或多条均拒绝。

接口**没有**按映射ID选择条目的字段。因此映射ID只用于追溯；重复组合必须先修正组织配置，不能用一个未发送给MES的参数假装消除歧义。

映射中的Owner、Role、ChecklistTemplate、FailureModeGroup等配置原样绑定到预览上下文和变更检查，不解释其流程、权限或执行结果。

## 提交和结果边界

请求严格限于原NCR、组织、分类、子分类、优先级和关联评论。工具不指定生成事件的名称或类别，不改失败代码，不传入容器替换、事件创建覆盖字段，也不设置关闭、流程或签名绕过参数。**不会额外调用NCRClose；原报告在升级后的最终状态由MES决定，仍待验收。**

非执行预览必须回显原报告及全部明确引用、完整事务评论，并加载与原报告相同的容器名称和ID。`EventDetail`可能是临时对象或尚未创建，不将其中临时ID绑定为已生成质量事件，也不把缺失的事件明细当成已生成结果。

组织电子签名设置和服务返回的ESigRequirement供用户审阅。`signature_verified=false`、`dynamic_validation_verified=false`、`created_event_verified=false`；准备成功不是权限、签名或审批已通过。执行仍由MES校验原生规则，本轮不实现电子签名采集。

准备末尾重读原报告、失败代码、原容器及参考配置；执行前重建计划摘要。状态、代码、分类映射或优先级变化即阻止提交。数据库ChangeCount与重读只能检测可见变化，不提供原子快照，也不锁住其他MES客户端。

未知升级占用同一NCR，并与关联容器的检验事务互斥，不能切换NCR更新/关闭或检验工具绕过。此互斥仅在本MCP质量事务域内生效。

`inspect_quality_transaction` 展示当前原报告（包括可能出现的 `EscalatedEventId`）及有限历史；当前关联相同、HTTP200或空历史均不证明本次升级成功。它不自动解锁或重试，最终证据由人工审计。现有 `find_quality_execution_candidates` 和 `reconcile_quality_execution` 未扩展到NCR升级，不能用检验历史核对代替其最终因果证明。

## 验证依据和限制

已现场读取正式NCREscalate Swagger、Query EDM及SQL列，验证组织映射展开与ChangeCount查询。EDM明确NCR状态1为Open、2为Closed，另以EscalatedEvent关联判断是否已经升级。

无报告目标的非执行预览返回HTTP200，仅得到系统默认简述；它不是完整升级计划或正目标验证。新组织选项工具在Corporate组织上成功返回完整空映射，现场优先级列表也为空。因此本轮未创建NCR、组织映射或优先级样本，未执行MES升级或其他业务写入，未验证真实事件生成、原NCR最终状态及审批/签名流程。

同时修正既有NCR失败代码关联对SQL整数文本的读取：`FieldId`和`Sequence`使用严格整数解析，接受现场的十进制文本，同时拒绝布尔、负数、错误字段、重复或错误父级。不放宽原有失败代码完整性限制。

新增68项NCR升级测试；全量1986项测试、2项子测试通过，其中质量相关780项，父pytest离线HTTPX真实网络尝试0。测试覆盖与全量结果见[本轮摘要](quality-fixtures/ncr-escalation-summary.json)。MCP协议多轮调用使用模拟业务响应，问法验证只检查工具可见性，不是真实LLM多轮选工具成功率测评。
