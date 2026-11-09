# 第5部分：抽样批整批报废完成记录

2026-11-09，第5部分在限定范围内完成。正常报废已有 C4 记录；本次新增**报废分支独立冻结成员拒绝验收**，并用另一个独立批完成正式计划执行、原生历史匹配和最终效果结案。没有改动生产批、生产配置或数据库业务表。

## 操作流程

1. `get_sampling_lot_scrap_readiness(lot_name, loss_reason_name)` 完整读取批及成员，核对实际 LossReason ID、各成员原因组和原生全量损失/关闭/生产数量默认值。省略原因时只展示待明确项。
2. `prepare_sampling_lot_scrap` 生成不可变计划，绑定原批、所有成员、数量/单位/工序、原因和原任务基线。
3. `execute_quality_transaction` 重建核对，正式只提交一次 `ExecuteSamplingLot`，结果码3。成员 ChangeQty 只用于非执行预览，不能循环扣量替代整批事务。
4. `reconcile_quality_execution` 核对唯一原评论、尝试时间、父批历史、全部成员损失历史及数量/原因/生产数量选项。
5. `review_quality_transaction(action="resolve_history", conclusion="committed", evidence_reference=原生TxnId)` 重读历史和全部原对象效果后结案。

`ready_for_execution=false` 表示只读检查不能批准或执行本次事务，**不表示整批报废未实现**。成功回复、当前余额相同、没有查到历史，也都不能单独作为提交成功或未提交的结论。

## 现场验收

前缀 `CDX-L5-20261109`，11项独立建模配置、2个 SamplingLot、4个显式 Container，共17个根对象。引用现有 BOX/EACH、IQC_DEMO_LVL/IQC_DEMO_AQL，仅作只读引用。工单为 `…-ORDER`，产品 `…-PRODUCT/1`，每个批数量5 EACH，由两个容器的2、3 EACH构成；Start前逐项核对产品版本、容量、BOX层级及上下文唯一Owner/StartReason。Owner=PRODUCTION、StartReason=NORMAL，显式命名、autoNumber=false。

| 场景 | 实际结果 |
|---|---|
| `…-NEG` 旧计划防护 | 准备计划后冻结 N02，执行旧计划返回 submitted=false；尝试表新增记录为0，未提交整批事务。 |
| `…-NEG` 原生报废拒绝 | 专用隔离验收脚本提交一次结果3请求，MES返回 HTTP400 / 13500440，明确指出 N02 冻结。冻结基线与拒绝后两次完整读回一致：批头/批任务、N01和N02原记录、成员采样任务、主历史、数量汇总/明细及批处置历史均未变；原请求评论无最终主历史。 |
| 测试冻结恢复 | N02通过原生Release解除本次测试冻结。N01/N02仍Active、分别2/3 EACH，两者冻结数均0；没有重新发报废请求或补偿改数量。 |
| `…-POS` 正式整批报废 | 原生事务 `400000000001041b`。P01损失2、P02损失3 EACH，两者归零并关闭。一个批父历史、两个成员 ChangeQty及损失明细；倍率-1、RecordAllQty/AdjustThruput/CountsAgainstProduction均true，LossReason ID一致。原生历史及完整最终效果核实通过，计划状态history_committed。 |

正式成功计划：`83e592b4aba04950bbd16586c1b7ccb4`。本地原始证据为 `tmp/lot-scrap-acceptance/manifest.journal.jsonl`，清单为同目录 `manifest.json`；日志、质量计划数据库及临时测试产物不纳入版本控制。

冻结拒绝由隔离脚本刻意调用原生校验，用于验证MES处理，与正式MCP执行旧计划的前置阻断分开记录。脚本只允许清单中的新对象和完整批，不能用于绕过生产计划约束。失败请求不伪造事务ID、不伪造人工审核，也不依据无历史自动解锁。

本次只证明**这个冻结成员拒绝场景未观察到部分提交**；没有证明第一个成员曾完成内部扣量，也没有证明所有超时、断网、崩溃、外部集成或数据库故障均能原子回滚。任何真实提交返回错误或未知，正式计划继续unknown占用，禁止自动重放。

## 本轮补齐的实现

- 报废前置校验使用报废业务提示，避免无拒收证据时误提示“返工”。任何成员存在未核实的吞吐/采集尝试时，阻断整批报废。
- 最终核对除原成员余额、关闭和损失历史外，还必须保持原批名称/类型、产品、主单位、抽样计划及批量；完整原成员集合及归属保留，成员不得新增冻结/返工/层级/载具关系。
- 区分两种任务：**成员 ContainerSampleData 应清理，批级 LotSampleData 保留原任务与累计值**。多出/缺失成员、变动批任务、换单位/产品均拒绝结案。
- 强制系统生成的32位事务标记，保留单次提交和unknown防重；能力目录、只读就绪说明和外部MCP引导同步到当前实现。

## 范围及复验

当前支持完整1..20个基础Container成员，Active、无冻结/返工/父子层级/载具/第二单位，同原批产品且主单位明确，MES确有拒收超阈值证据。原因以当前部署实际接受的LossReason持久ID绑定，不用同名ScrapReason替换；所有成员的当前Operation原因组均须包含它。拒绝全检/失败转全检任务、额外签名和未覆盖组合。

原生关闭、生产数量调整及解关联等选项由原生批事务默认值控制，准备时逐成员完整回显，不自行覆盖成另一套数量规则。抽样批8587与真实Batch7939是不同对象，不能互相代替。

复验入口 `scripts/lot_scrap_acceptance.py`：先用 `--prefix CDX-L5-新的唯一前缀 --output 临时目录` 仅生成并核查清单，再以 `--apply 清单路径` 执行。阈值不足17、同名对象存在、原批成员不完整、未知写入或系统错误时停止；每次写入前持久化日志，不自动重试，也不领养同名对象。测试对象及失败批保留供复核。

## 最终检查

相关回归661项通过；1项旧CAPA回显测试因缺少历史样本单独排除。两个旧抽样批契约测试缺失的本地样本已通过只读获取当前MES的CollectLotSamplingData、ExecuteSamplingLot及ChangeQty契约补齐，契约校验通过。

8030已加载当前版本，外部MCP实测332个工具，G10状态为implemented；对NEG批带明确LossReason的只读就绪检查通过、不创建计划或写MES，POS原计划读回history_committed且verified_success=true。WorkBuddy重连后可使用现有专用工具。

验收脚本遇到原生结果未知或未核实拒绝时，会保留测试冻结并停止，不在可能仍执行的事务期间自动Release；本次已取得明确拒绝及稳定证据后才解除测试冻结。
