# 第1部分：生产过站与报工

2026-11-08。六项核心功能已实现，并使用独立测试数据完成原生写入及历史核对。验收范围：同一吞吐层级、无父子/载具/返工/冻结的基础容器，以及明确的分步生产流程。

| 功能 | 入口 | 已验证行为 |
|---|---|---|
| 吞吐量 | prepare_container_throughput → execute_container_throughput → verify_production_transaction | 部分加工、剩余量、原生ThruputHistory、单次提交 |
| 进出站 | prepare_production_station_transition → execute_production_station_transition → verify_production_transaction | enter=MoveIn；报工后exit/complete=独立MoveStd，沿真实路径推进 |
| 工单进度 | get_mfgorder_production_summary；既有get_mfgorder_progress | 原生完工量、在制、按工序/单位的吞吐及完成日期；单位一致时显示原生完工数量比例 |
| 良品/不良 | prepare_production_reject；record_production_report；get_production_reports | 登记保留在制、报废扣量；声明件数与已核实原生计划关联 |
| 工时 | record_production_labor；人工工时提交四个入口；get_production_worktime | 人工实际分钟可落MES CollectData；进出站历时独立统计 |
| 整单完工 | station action=complete；get_mfgorder_completion_readiness → record_mfgorder_completion | 原生移入完工节点触发生产完成，读取MES日期后登记应用收尾 |

具体数量、对象及事务见[现场验收](production-acceptance.md)，契约来源见[结构化记录](production-reporting-contract.json)。

## 操作顺序

1. 查询实际容器ID、状态、工单和工艺路径。
2. 排队工序用action=enter准备、执行、核实原生进站。
3. 独立Thruput明确本次加工量=良品+不良，可以分次上报；执行后核实唯一原生历史。
4. 有不良时选择record或scrap，准备、执行并核实原处置。
5. record_production_report保存不可变声明台账；每个原生计划只被一条报工记录消费，同编号/内容幂等。
6. 人工分钟需要落MES时，在原工序提交并核实人工工时采集计划。
7. 剩余未加工量为零后，明确真实路径出站。普通目标节点选exit；移入IsLastStep生产完成节点必须选complete。
8. 全部容器按工艺完成后，核查MES生产完成日期、后置过程、未完成在制及未核实提交，再登记应用整单收尾。

各步为独立事务，不是跨步骤原子批量。中途失败不自动重试、回滚、补报、跳站或改用其它事务。原生签名、培训、采集、调度及计时校验保留；准备不等于通过动态写入验证。

## 出站与整单完成

原生复合MoveOut在本部署实测返回13500597：Transaction Details requires input。测试C02失败后停止，没有重试或改用其它事务。

已验收的分步流程明确采用MoveIn、独立Thruput、独立MoveStd；它不是失败后的自动替代。新计划同样纳入应用内同一请求的移动失败限制；本机unknown/in_flight提交阻止后续报工及移动。外部MCP客户端须保持相同授权与失败停止要求。

现场确认：移入IsLastStep时MES即设置ProductionComplete，该节点不能再当待加工工序重复进站。complete沿原路径移动，close=false，不直接写OrderStatus或完成日期。

整单生产完成与独立业务关闭分别报告。本部分实现原生工艺触发的生产完成和其后的应用收尾，不把修改Closed、日期或任务状态冒充生产完成。

## 两种不良处置

| 选择 | 原生事务 | 数量影响及核实 |
|---|---|---|
| record | execute_production_reject执行ContainerDefect | 保留在制；明确不良件数、检查数及缺陷次数；verify_production_transaction核对DefectHistory及明细 |
| scrap | execute_quality_transaction执行LossDetails | 扣减明确数量；必须明确close_when_zero、adjust_throughput；沿用质量历史核对及resolve_history结案 |

良品/不良是操作员声明的件数，保存在data/production_records.sqlite3，并关联MES加工量和处置证据；不伪称MES自动质量判定。缺陷次数不等于不良件数。按工序和单位分别统计，不跨工序相加成为工单产量。

## 两种工时

record_production_labor要求明确员工、工单、带时区起止时间和实际分钟，可绑定容器。实际分钟须为正且不超过区间经过时间；最长区间7天；同员工重叠区间拒绝，团队逐人登记。

只登记本地时明确标为应用台账。需要MES落账时，先将记录绑定原容器，再使用：

1. prepare_production_labor_submission(record_id, definition_name, definition_revision, minutes_uom_name)
2. execute_production_labor_submission(record_id)
3. verify_production_labor_submission(record_id)
4. get_production_labor_submission(record_id)查询落账状态

原生CollectData专用User定义必须恰有七点并关联当前Spec的CollectData：

| 点名 | 类型 |
|---|---|
| LaborKey、EmployeeName、EmployeeId、MfgOrderName | String |
| ActualMinutes | Decimal；明确指定的分钟UOM |
| StartTime、EndTime | TimeStamp，输入带时区 |

通过已有采集维护工具配置，不自动修改现有生产配置。提交绑定原容器、工单、单位、工序和步骤次数；工序变化后不在另一工序补写。每条工时只绑定一个单次计划，不重复累计。该方案不猜TaskTime单位，也不执行或完成电子作业任务。

自动历时取MoveHistory和唯一真实MoveInHistory，支持独立MoveStd及对应MoveOut/MoveWithThruput子事务历史。必须同容器、工序、步骤次数、入步事务且正常未冲销。

本部署MoveInDateGMT截到整秒；按现场验证的精确/截秒规则关联唯一进站记录，实际历时使用两个原生事务的GMT时间，保留毫秒。缺进站、负区间、重复或冲销排除并计数。无历史不证明没生产；历时可能含等待/暂停，不等于人工劳动。

get_production_worktime分别返回人工分钟/小时、MES人工落账情况、自动历时及包含/排除数。两种小时数不相加；原生落账不会再次累计同一条人工记录。

## 数量和结果核对

原生定义：AdjustedThruputQty = CurrentThruputQty - ThruputAdjustmentQty；剩余量 = Qty - CurrentThruputQty + ThruputAdjustmentQty。

新容器计数可为空。代码保留空值，仅通过Thruput execute=false读取原生只读QtyRemaining确认剩余量，不本地设零，也不将其视为动态写入验证。

新计划含唯一评论标记，绑定实际16位Container ID、环境、配置、工序、步骤次数和历史基线。执行前重读、原子领取并只POST一次。HTTP成功、当前相同数量或空历史不足以证明本次成功。

verify_production_transaction双读唯一主历史和持久汇总/明细，核对服务、事务因果键、数量、单位、路径、原因及正常状态；成功保存history_committed并解除本机占用，原计划仍不可重放。无标记旧计划、未覆盖组合服务或证据不足时沿用最终证据人工审计。

证据表示核实时的事实。后续改绑、冲销、重加工和配置变化须另查原生历史；本地锁不是MES全局排他或原生幂等键，跨查询不保证一致性快照。

原生CompletedQty保留为原生完工量，不当作质量良品。当前原生ScrapQty可能统计关闭容器个数，不直接当报废件数，也不与CompletedQty相加计算良品。

现场已验证部分吞吐、两种不良处置、分步进出站、人工分钟落账、非空自动历时、整单生产完成和防重放。父子/载具、返工及复杂签名/培训/计时/必填采集路线未纳入本次现场验收；原有MoveWithThruput入口未作为本次整条路线验收。原生MoveOut服务端问题仍待独立修复。
