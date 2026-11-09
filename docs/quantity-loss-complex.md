# 第4部分：父子层级与载具数量损失

2026-11-09，已在 `CDX-L4-20261109` 独立测试对象上完成六笔原生 ChangeQty，均通过唯一事务历史、完整原对象状态及精确版本变化核实，状态为 `history_committed`。未修改生产工厂或账号默认配置。

## 使用入口

1. `get_quantity_loss_scope` 读取真实类型、完整活动父子层级、载具成员及跟踪容器双向身份。
2. `prepare_related_container_quantity_loss` 准备计划，明确损失原因、部分数量或全量、归零关闭、关闭解关联、生产数量调整，并要求 `generate_rollups=true`。
3. `execute_quality_transaction` 重核原范围后只提交一次原生 ChangeQty。
4. `reconcile_quality_execution` 匹配原标记、时间窗、事务类型和全部数量历史。使用 `review_quality_transaction(action="resolve_history", conclusion="committed", evidence_reference=原TxnId)` 核实当前原对象并结案。unknown 不重放。

独立基础容器继续使用 `prepare_container_quantity_loss`；SamplingLot 使用独立抽样批接口，不能按成员循环扣量模拟整批事务。

## 已验收行为

| 场景 | 实际结果 | 原生事务 |
|---|---|---|
| 子容器部分损失 | L1 5→3，PA 8→6，L2 保持3 | `40000000000103de` |
| 子容器全量并关闭 | L1 3→0/Closed，PA 6→3，活动 ChildCount 2→1 | `40000000000103e0` |
| 父容器全量级联 | PB 3→0，L3 2→0，L4 1→0，三者关闭 | `40000000000103e2` |
| 最后子容器全量，调整生产数量 | L2 3→0/Closed，PA 3→0但仍Active、ChildCount=0；原生 AdjustThruput=true，PA.ThruputAdjustmentQty=3 | `40000000000103e4` |
| 载具中部分损失 | CA 4→3，跟踪根4→3，成员/绑定保持 | `40000000000103f9` |
| 载具中全量并关闭 | CA 3→0/Closed，跟踪根3→0且仍Active；共享 CurrentStatus 的载具绑定清除 | `40000000000103fb` |

历史中的父子关系有两个口径：关闭对象仍保留历史 ParentId，活动 ChildCount 不包含关闭对象。`DisassociateWhenClosed=true` 不能被解释为这个部署一定会清空历史父ID。整棵子树全量损失，MES保存目标父容器的合并直接明细，不为每个叶容器另造一条 QtyHistoryDetails；程序分别核对原生合并历史和所有原对象效果。

根历史的 HistoryId 属于顶层容器，QtyHistory 的 HistoryId 属于实际事务目标；祖先汇总明细是 CDO5020、负数Qty、倍率+1，直接损失明细为 CDO6932、正数Qty、倍率-1。两者不可混算为额外损失。`LastRevTxnId` 可能为空或保留旧移站事务，不是 ChangeQty 事务令牌；核实必须同时满足本次原生历史和恰好一次 ChangeCount 增量。

生产数量调整由 MES 处理。上述最后子容器测试的调整量落在顶层 PA 上，叶容器字段为空，不能据此伪称叶容器计数已更新；该测试没有事前生产计数基线，只报告实际观察值和历史选项。空计数不擅自按零补齐。

## 工厂载具配置已通过 API 完成

隔离工厂 `CDX-L4-20261109-FACTORY` 已配置编号规则 `…-CARRULE`、父容器层级 `…-PARENT`、流程 `…-WF/1`、Owner=PRODUCTION、StartReason=NORMAL、容量2和启用载具跟踪。

标准 REST FactoryEntity 未发布这些扩展字段，使用同一 MES 账号的原生 `FactoryMaintService.svc` 完成。按现场 WSDL 和本地 WCF 手册第2章维护示例，单个 CommitTransaction 包含 Load 与 ExecuteTransaction，随后 Load 回读全部目标字段。凭据只在内存中的认证封套中使用；没有数据库写入、权限变更或元数据发布。

开工的载具字段是 `CurrentStatusDetails.Carrier`，顶层 Carrier 不能作为实际绑定成功的证据。现场已有测试容器通过正常 ContainerMaint 的 ServiceDetail.Factory 归属隔离工厂，再由原生 `isLoadUnloadCarrierService.svc` 装载；装载后的父子关系、共享状态、CarrierId和跟踪根均已实际回读。空跟踪根允许没有产品/单位，但必须由载具生产状态和容器反向引用同时证明；所有实物成员仍须同产品、同主单位、同步骤。不能用空根放宽混合成员检查。

## 明确边界

- **真实 Batch CDO7939：MES 原生 ChangeQty_Validate 明确禁止。** 保持明确拒绝，不改对象类型、不直接改表、不用 SamplingLot 或循环成员扣量替代。
- 完整相关范围最多8个容器，覆盖活动祖先、子孙及载具成员；不按分页片段判断影响。各实物容器须为基础1040、Active、无冻结/返工/抽样批、同产品/主单位/步骤。
- 叶容器支持明确部分损失；有子容器的父容器仅支持明确整棵目标子树全量，未开放猜测部分分摊。
- 复杂层级/载具限单单位；独立双单位部分损失继续使用已验收的基础入口。载具执行限双向身份完整的原生跟踪载具。
- 归零会按原生逻辑清除共享状态的载具绑定，预览明确列出全部受影响对象；活动范围外历史成员若也共享受影响状态，阻断提交，不能静默改变其他成员。
- 旧v1/v2实验计划保持原请求、摘要和原投影不变；原生父ID/合并历史语义的修正单独记录在审计证据，不能改计划或重复提交。

## 复验与证据

`scripts/quantity_loss_acceptance.py` 先生成仅查询的独立清单，再显式应用；全流程20个顶层对象包含11项建模、7个显式容器、1个载具资源及其1个自动跟踪容器。失败停止，禁止领养同名对象或重放。Start 前核对工单产品版本、层级和上下文选择值。WCF桥仅允许 `CDX-L4-*` 隔离配置和固定字段，不是任意服务代理。

本次原始日志：`tmp/quantity-loss-acceptance/journal.jsonl`，不可变质量计划保存在现有质量计划数据库；日志和数据库均不纳入版本控制。测试对象保留以供核查，没有清理或删除生产数据。新增自动化检查覆盖原生历史符号/类型、混合单位/产品、层级循环、共享状态脱离、陈旧移站令牌、作用域冲突、计划篡改以及WCF单次提交与配置隔离。
