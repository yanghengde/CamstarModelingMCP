# 已升级NCR的质量侧收尾

2026-10-28新增 `prepare_quality_ncr_closure`，封装正式Shopfloor `NCRCloseQuality`。质量工具共47个，执行、检查和最终证据审计继续使用现有统一事务工具。

## 本工具的保守范围

仅处理**已关联质量事件，而原NCR仍Open**的收尾请求，并要求：

- 原报告字段及失败代码完整，NCR状态1且关闭时间为空。
- 沿原报告的 `EscalatedEventId` 精确查询Event表，关联记录存在，状态5（Closed），有非空关闭时间和有效格式的关闭人ID。
- 原关联容器Active；已有冻结可以保留，不调用解除冻结。
- 明确选择NCR解决代码，服务预览完整回显原报告、解决代码和原容器。

这些条件是本工具为限制操作范围而采用的前置条件。**不表示所有Camstar部署都必须在事件关闭后手动调用此服务**；已有原生流程可能已经完成NCR关闭。报告已Closed时本工具拒绝重复准备。

不能把Pending、InReview、Escalated或Deleted当成Closed。本工具不会替用户关闭或修改质量事件，也不执行事件审批、签名、流程推进或强制关闭。Event状态枚举来自现场EDM：1 Active、2 Pending、3 Escalated、5 Closed、6 Deleted、7 Initiated、8 InReview。

## 参数与调用

先用 `get_nonconformance_report` 查看原报告及关联事件；必要时用既有质量对象查询工具查看事件详情。明确收尾请求后：

```json
{
  "report_name": "仍Open的已升级NCR名称",
  "resolution_code_name": "明确NCR解决代码",
  "comments": "明确的质量侧收尾说明",
  "ncr_comment": "可选的NCR备注"
}
```

解决代码必须来自 `list_quality_references(kind="ncr_resolution")` 或对应命名对象。它是 **NCRResolutionCode**，不能自动复用质量事件上的 **QualityResolutionCode**。

根评论最多180个UTF-16单元，系统附加事务标记；可选NCR备注最多4000字符且不含NUL。省略备注时不发送对应修改字段。

准备成功后审阅原报告、原事件、容器、解决代码和最终请求，由用户确认同一 `plan_id` 与 `plan_digest`，再调用 `execute_quality_transaction`。普通制造侧关闭继续使用 `prepare_nonconformance_report(action="close")`；两者不能自动互换，质量侧入口拒绝后不能自行降级到普通关闭来绕过前置条件。

## 请求与变化保护

请求只包含 `nonconformanceReport`、`comments` 和 `serviceDetail`，明细只允许 `ncrResolutionCode` 及可选 `ncrComment`。正式类型为 `NCRCloseQualityDetail`，继承 `NCRCloseDetail`。

不发送质量事件替换、事件状态、失败代码、dirty字段、容器替换、签名/流程绕过等字段。服务、计划动作、原报告、解决代码与快照双向绑定；不能修改计划改用其他服务。

非执行预览使用显式明细字段选择，核对完整输入和服务派生的原容器名称及ID。没有强制依赖接口未公开的动态CDOTypeId，也不会把临时明细ID绑定为持久化对象。

准备末尾重读原报告、失败代码、原关联事件、原容器及解决代码；执行前再次重建摘要。事件被重新打开、报告换了关联、报告已经关闭或解决代码变化，均阻止旧计划提交。此保护只检测可见变化，不保证数据库原子快照，不锁住其他MES客户端。

未知事务继续占用原NCR和关联容器，并声明原事件读取依赖。不能用新计划、普通NCR关闭、升级或同容器检验绕过本MCP质量域中的未核实占用。

## 结果不是自动成功

HTTP200仍保存unknown，实际提交只有一次，不自动重试。`signature_verified`、`dynamic_validation_verified` 和 `ncr_closed_verified` 均为false；前置Closed事件不是当前收尾的权限或签名证明。MES仍负责全部原生校验及后续处理。

`inspect_quality_transaction` 显示当前原NCR、**原来关联的事件**当前记录，以及当前报告是否仍指向该原事件。报告关联被改写时不自动跟随新事件。即使看到NCR已关闭、事件仍Closed，也不能归因于本次请求，所有成功、解锁、重试和历史关联验证标志保持false。

最终结果需人工结合MES最终审计证据使用 `review_quality_transaction` 核实。现有自动检验历史候选与核对工具没有扩展到此NCR服务；原计划不可重放。

## 现场与测试证据

已现场读取NCRCloseQuality/NCRClose Swagger、Query EDM、Event与NCRResolutionCode列元数据。无目标 `execute=false` 预览返回HTTP200，但只有空ServiceDetail及评论，不能用于批准实际关闭。

完整Event字段查询、已升级NCR查询和解决代码列表查询均返回成功；前两者TOP1为空。本轮没有创建质量事件/NCR测试数据，也没有执行MES关闭或其他业务写入。真实目标预览、关闭结果、原生副作用及最终因果历史仍待验收。

新增64项离线测试覆盖Closed判别、原关联身份、关闭证据、备注、非执行回显、依赖变化、执行白名单、unknown互斥、原事件追溯和真实MCP协议下的模拟准备/执行/检查。问法检查只验证工具可见性，不是真实LLM多轮选工具成功率评测。

全量2087项测试、2项子测试通过，其中质量相关881项；父pytest离线HTTPX真实网络尝试0。结果见[本轮摘要](quality-fixtures/ncr-quality-close-summary.json)、[接口及查询依据](quality-fixtures/ncr-quality-close-contracts.json)、[无目标预览](quality-fixtures/ncr-quality-close-empty-preview.json)。
