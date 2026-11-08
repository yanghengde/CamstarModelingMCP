# 生产过站与报工

2026-11-08。已接入独立加工量上报、带吞吐移站、实际吞吐历史和全工单生产证据；按用户选择新增不良登记/报废扣量、本应用良品与不良台账、人工工时与进出站历时分别统计、MES完成后的本应用整单收尾。
现场仅进行了契约和查询联调，没有执行生产事务。人工工时保存在本应用，不写未经核实的MES TaskTime；独立MES整单关闭尚无已验证契约，不能称为MES六项写入全部验收。

契约与现场只读证据见[核查记录](production-reporting-contract.json)。

## 入口

| 功能 | 工具 | 当前范围 |
|---|---|---|
| 进站、出站 | `container_move_in` / `container_move_out` | 沿用已有原生事务；MoveOut组合事务及完整路线仍待现场验收 |
| 独立上报本次加工量 | `prepare_container_throughput` → `execute_container_throughput` | `service=Thruput`，明确正数qty，可部分上报，不移动工序 |
| 带吞吐移站 | 同上，`service=MoveWithThruput` | 仅未进站的基础容器、明确全部剩余加工量，不代替已进站的MoveOut |
| 单容器加工历史 | `get_container_throughput_history` | 实际ThruputHistory主记录，保留事务、工序、单位、StepPass及冲销标志，明确分页 |
| 工单生产汇总 | `get_mfgorder_production_summary` | 当前关联容器、按工序/单位/冲销状态分组的原始吞吐、原生产量及生产完成时间 |
| 提交结果核对 | `get_container_throughput_plan` | 原始请求、响应及提交后的历史回读；同值或HTTP成功不自动作为本次提交归因 |
| 中断恢复 | `recover_container_throughput_attempt` | 本机执行结束后in_flight转unknown，保留占用，不证明MES请求结束 |
| 最终结果审计 | `review_container_throughput` | 明确原生最终事务/日志证据后审计unknown；旧计划永不可重放 |

新入口已纳入MCP注册、Agent工具分发、能力目录、自然语言路由与确认摘要。
部署时重启服务并刷新客户端工具清单。

## 两种不良处置与良品/不良报工

报工时由调用方明确选择，选择不保存在全局配置中：

| 选择 | 准备与执行 | 数量影响 |
|---|---|---|
| `record` 不良登记 | `prepare_production_reject` → 返回的`execute_production_reject` | 原生ContainerDefect，保留在制数量 |
| `scrap` 报废扣量 | `prepare_production_reject` → 返回的`execute_quality_transaction` | 复用原生LossDetails，扣减明确数量；归零关闭和吞吐调整必须明确选择 |

登记模式分别提供`reject_qty`不良件数、`inspected_qty`本次检查数量、`defect_count`缺陷次数；一件可能存在多个缺陷，不自动推断相等。报废模式复用已有原因组、主单位、子类型和原生回显约束；全量自动选用明确的RecordAllQty计划，不以普通类型数字模拟报废。报工封装额外绑定原工单、持久状态、工序、步骤次数与单位，执行时变化阻止旧计划。

完整报工顺序：先准备并提交本工序独立Thruput，数量为本次良品加不良；按最终证据核实后再准备并提交不良处置。再次核实后调用`record_production_report`保存本应用台账。任一步未知立即停止，不自动继续或回滚。`disposition=none`仅用于不良为零。

报工台账必须绑定已核实提交的原计划，核对数量、容器、工单、主单位、工序及步骤次数。每个原生计划只能被一条报工记录消费，报工编号及相同内容幂等，不可覆盖旧记录。
`get_production_reports`按工序和单位分别统计良品、登记不良、报废数量；不跨工序累加为工单产量。良品/不良为操作员明确声明并与原生提交关联，不伪称MES自动质量判定。

## 两类工时分别保留

- `record_production_labor`：按明确员工、工单、带时区起止区间及实际分钟保存人工工时。可核对容器归属，实际分钟不能超过区间经过时间，同员工重叠区间拒绝重复计算；团队逐人登记。原始输入与100纳秒时间刻度均保留。
- `get_production_worktime`：分别返回人工台账的全量分钟/小时与MES自动历时。人工记录分页不改变全量总计，两类小时数不相加。

自动口径仅取原生MoveOut主历史的`MoveInDateGMT`到`TxnDateGMT`，按当前工单关联容器统计；正常未冲销状态为1，同容器同事务必须唯一。缺失日期、负区间、冲销和重复主记录排除并计数。采用SQL Server DATEDIFF_BIG微秒差，避免猜测原生Duration/ElapsedTime字段的单位。
没有MoveOut历史表示未取得相应区间证据，不证明实际工时为零。仅MoveStd或无MoveIn的MoveWithThruput不混入自动加工历时。历时可能含等待、暂停，不等于人员实际劳动时间。

台账文件为`data/production_records.sqlite3`，按Modeling/Query/Shopfloor环境隔离，数量以十进制文本保留。人工工时没有写入MES TaskTime，其未知单位不被默认解释成小时。

## 整单收尾

`get_mfgorder_completion_readiness`核查原生生产完成日期、已配置后置过程的完成日期、未完成Active容器，以及本应用未核实的报工/报废提交。
只有证据齐全，`record_mfgorder_completion`才登记本应用收尾，同一原生完成日期不可重复登记。未记录生产完成日期或仍有未完成在制时拒绝保存。
此入口不向MES发送关闭事务，不修改OrderStatus或生产日期。实际MES完成仍由原生工艺规则触发，独立MES业务关闭须另行查明契约。

## 数量与完工口径

`qty`表示本次加工量，不表示余额、良品量或不良品量。只支持工序吞吐上报层级与当前容器层级相同、无父子关联、Active、未冻结、尚未生产完成的基础容器，不修改层级或绕过原生配置。

本地原生元数据定义为：

```
AdjustedThruputQty = CurrentThruputQty - ThruputAdjustmentQty
剩余未加工量 = Qty - CurrentThruputQty + ThruputAdjustmentQty
```

此处调整量的符号不可写反。容器实际身份来自状态中的Container引用，不能使用临时状态视图行ID。
缺失/异常的加工量计数不作为零；冻结计数沿用已有数量处置查询的显式`COALESCE(CurrentHoldCount,0)`契约。
Query/AdHoc的数字和布尔字符串按严格规则解析。

原生`MfgOrderInquiry_GetOrderQuantities`已通过现场只读调用，返回`CompletedQty`、`ScrapQty`、`OrderQty`。
汇总将`CompletedQty`单独标为原生完工量，保留原始响应；不把它当作质量合格数，也不把`ScrapQty`当作不良件数。
本地原生定义中的`ScrapQty`统计Status=2容器的个数，而`CompletedQty`累加ProductionComplete容器的Qty；这说明两者不能直接按统一单位相加。
本地副本与现场完整业务定义的一致性尚未证实。

本地原生`MoveTxn_SetMfgOrderProductionComplete`会在原生既往完工量、本次完工量与报废量达到工单数量时设置生产完成日期。
汇总回读现场`ProductionCompleteDateGMT`及后置过程时间，不直接调用内部CLF、不写数据库、不通过修改OrderStatus或日期伪造完工。
生产完成时间、后置过程完成和整单业务关闭分别报告。没有独立的已核实整单关闭契约。

按工序统计的吞吐量含多次过站/重加工或可能的冲销记录；主历史与明细不重复累加，不跨工序求和成为工单产量。
工单统计限定为当前关联容器，不能据此追溯历史改绑前的订单归属；多次查询不保证事务快照。

## 提交和结果核对

准备阶段只有只读查询和本地静态计划，不调用Shopfloor的execute=false预览，也不声称已通过动态校验。
执行前重读身份、状态、数量、配置和历史基线；任一变化使旧计划失效。
计划绑定Modeling读取所得实际Container、Shopfloor及Query环境、不可变摘要与一小时时效。
领取时通过SQLite事务互斥；领取后只发一次POST，保持产品原生签名、采集、调度、计时及培训校验，不传绕过开关。

请求一经尝试保留unknown或in_flight，不自动重试、回滚或改用其他事务。
同一容器的未核实吞吐请求阻止后续本模块提交，也阻止现有普通MoveStd/MoveIn/MoveOut入口绕过占用。
中断恢复保留占用；最终人工审计才释放占用，但原计划一直不可重放。
这是本应用本地保护，不是MES原生幂等键，也不阻止其他客户端同时操作现场。
外部MCP客户端仍负责身份、业务授权和适当的人工确认，计划ID/摘要不是授权凭证。

## 仍需完成

| 项目 | 缺少的业务/现场证据 |
|---|---|
| 原生报工验收 | 代码已支持两种不良处置和关联台账，仍须现场核对实际数量、原因、历史与提交结果 |
| 工时 | 人工台账已实现；非空MoveOut区间尚待验收，MES TaskTime的单位、持久化及回写入口仍未核实 |
| 整单完工 | 本应用收尾核查/登记已实现；原生完成触发链和独立MES关闭仍需现场契约与完整路线验收 |
| 写入验收 | 在明确的测试容器/工单验证部分吞吐、剩余量、带吞吐移站、历史关联和重复提交处理 |

## 验证

离线测试禁止真实HTTPX网络；覆盖数量及调整符号、严格字符串解析、引用身份、未进站约束、临时视图ID变化、不可变计划、环境变化、单次提交、并发、取消、中断恢复、最终人工审计、分页、原生产量、注册与路由，并回归原移动/审批/工单查询。

相关回归429项通过，启用HTTPX网络阻断且真实网络尝试为0。只读现场验证：工单汇总、容器吞吐历史、双口径工时和收尾条件可读，BOX-0001本次加工量1的静态检查通过，扩展字段及重复读取摘要一致；没有创建执行计划或提交生产写入。示例工单无MoveOut区间，未将空历史当作真实零工时；其80个Active未完成容器阻止整单收尾。

可复用只读联调命令：

```powershell
venv/Scripts/python.exe scripts/validate_production_reporting.py --mfgorder MO-100076435 --container BOX-0001 --container-instance-id 0004108000000002
```

该脚本只GET契约及调用固定Query查询，不执行生产写入、不创建执行计划。
