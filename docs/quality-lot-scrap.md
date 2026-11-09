# 原生整批报废

2026-11-09更新：第5部分已完成独立正常报废与冻结成员原生拒绝验收，并强化完整原批/成员/两类任务的最终核对。详见[当前完成记录](lot-scrap-completion.md)。下文保留2026-11-04阶段依据及历史测试数量。

2026-11-04，C4集中交付。新增 `prepare_sampling_lot_scrap(lot_name, loss_reason_name, lot_instance_id=None, comments='')`，正式事务使用统一确认入口 `execute_quality_transaction`。

## 原因绑定依据

标准元数据将根字段命名为ScrapReason，并声明其旧类型。现场只读Load实际接受LossReason持久ID，返回相同ID和 `CDOTypeName=LossReason`。子LossDetails接受同一LossReason ID。本实现每次准备及执行重建时复核该绑定，保持实际对象类型和ID。

原因由LossReasons集合明确解析，必须同时属于每个成员当前Operation的完整LossReasons组。名称相同、相似或独立ScrapReason对象均不足以通过绑定校验。

## 当前实现范围

入口为Active、MES未通过且有任务拒收超阈值证据的SamplingLot，全部1..20成员必须完整可读。成员为基础Container，Active、无冻结/返工/层级/载具/第二单位，主单位和正数数量明确。成员产品与原批相同，工序/步骤与持久CurrentStatus一致。全检/失败转全检任务和其它复杂组合沿原限制处理。

每个成员仅通过execute=false ChangeQty探测原生全量LossDetails，核对类型4030、方向-1、RecordAllQty=true、实际原因/容器及计入生产标志，保留原生CloseWhenZero、DisassociateWhenClosed、GenerateRollups、RecordAllQtySummaryOnly和AdjustThruput默认值。预览逐成员展示这些默认值、单位、当前量和预计余额0。

完整成员、数量、单位/工序/原因配置、原采样任务及原生类型绑定都纳入不可变计划。读取期间或执行重建时变化会拒绝提交。根报文仅包含原批、结果3、原因和评论。

正式只执行一次ExecuteSamplingLot。成员ChangeQty调用全部为非执行探测。原生批事务负责内部数量损失、按默认关闭及成员ContainerSampleData清理。

## 异常与审计

超时、HTTP400和成功回复均先保存unknown及原批/原成员占用，禁止自动重放。inspect按原ID读取余额、单位、关闭状态及任务清理，复读后返回证据。部分观察到的效果逐成员展示，不能将其归为整批成功。

隔离批 LS 的最终父子事务关联已验证。最终审核沿 `review_quality_transaction`，仍须提供具体事务证据。

## 验证与剩余工作

全项目4424项及2项子测试通过，离线HTTPX真实传输尝试0。专项覆盖具体原因类型、完整成员、原生默认值、错误子类型、签名要求、配置/数量变化、确认、未知防重及部分结果可见性。现场已验证LossReason ID的Load绑定和独立非执行LossDetails默认值。

隔离批 LS 的两名成员均已扣量至零并关闭，原成员采样任务已清理。MES 历史确认同一事务下两笔成员损失，原因均为 SCRAP，并记录了生产数量选项。负向验收覆盖 LF 的整批返工失败，报废分支的独立失败注入后来已于2026-11-09补齐，见上方当前完成记录。详见[隔离验收记录](c4-isolated-acceptance.md)。

本地证据包括 `docs/quality-fixtures/c4-reason-load-probe.json`、`c4-scrap-child-live-preview.json`、`c4-completion-live-read.json`、`c4-final-junit.xml` 和 `c4-final-network.json`。
