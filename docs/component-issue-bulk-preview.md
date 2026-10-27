# 模式2批量容器来源校验与非执行回显

日期：2026-10-25（Asia/Shanghai）。只读工具`preview_bulk_component_issue`完成一个来源容器、一个目标容器、单条未消耗工单需求的校验及可选`execute=false`序列化检查。本预览不生成计划或执行库存扣减，`executable`与`complete`恒为false。最新正式准备/执行/核实/恢复及G现场验收见[共用单次计划](component-issue-shared-plans.md)；以下保留F契约实验过程。

同日执行实验续接：已通过实时EDM确认IssueDetailsBulk/ActualsBulk派生类型，并在既有F样本提交一次2 EACH，来源3→1，目标已投0→2/净需求2→0，四表来源历史链核实成功。该写入仅来自固定F样本、永久日志防重放的诊断脚本，不是通用MCP执行工具；本轮未创建MES对象。

## 已实现范围

- 必须明确目标/来源名称、产品名及固定版本和严格有限正数，可选明确双方16位ID；拒绝自身投自身和身份不一致。
- 双方状态查询须完整、Active且InProcess，明确非返工；目标当前工单/Spec/Operation/Step引用须完整。
- ComponentIssueInquiry通过来源容器名称匹配目标需求，核对ParentContainer、ObjectName、模式2、产品ID/版本、所属工单需求行及单位。只支持首次完整需求；已有消耗/部分量/待处理实际量/退料/第二需求量均拒绝。
- 来源状态产品必须与需求固定版本一致，来源单位必须相同，不猜替代料、不做换算。独立固定查询核对来源ID/名称/ProductId/UOMId/Status/Qty，且与状态数量一致、数量不超余额。
- 保留Qty2/UOM2Id/ParentContainerId三个原始列，缺列拒绝；无第二单位且第二数量为空或0、父关系明确为空才继续。查询当前子容器，要求来源是根叶子容器。空字段来自明确投影，不把被Inquiry省略的字段猜为0或无父关系。
- 只有上述校验通过且调用方明确`verify_serialization=true`才调用固定ComponentIssue端点，参数始终`execute=false`。校验目标、防超量标志、唯一需求行、唯一来源实际明细、嵌套实际量的产品/目标/单位/需求行所属对象及全部数量。

当前请求已更新为一条`#shopfloor.IssueDetailsBulk`需求行、一个`#shopfloor.ActualsBulk`实际记录，FromContainer位于该派生Actuals内；不再同时发送顶层IssueActualDetails。回显还核对实际类型ID6540与来源引用。下文“两组实际量”的预览记录为前一阶段快照。

数量预测为观察余额减本次数量，仅供核对，不是库存预留。只读、逐次观察不能证明未冻结、现场可消费或其他客户端未改变状态。

## 当前部署及真实证据

当前Shopfloor可达，Swagger与既有归档仅`servers`地址不同，脱敏契约相同；没有沿用旧部署的本地计划。见[当前契约](t05-fixtures/bulk-current-deployment-contract.json)及[来源列检查](t05-fixtures/bulk-source-column-check.json)。

按Container Start技能预检并创建F批6个隔离对象，低于当前安全阈值20，全部创建后回读确认：

| 类型 | 名称 | 内容 |
|---|---|---|
| 产品 | CDX-T05-20261025-F-COMP / 1 | EACH，复制明确WF-WM-900A/A关联以供来源Start |
| 产品 | CDX-T05-20261025-F-TARGET / 1 | EACH，WF-WM-900A/A |
| 工单 | CDX-T05-20261025-F-SRC-MO | 来源产品，数量3 |
| 工单 | CDX-T05-20261025-F-DST-MO | 目标产品，数量1；T05-BULK需求行模式2、2 EACH |
| 来源容器 | CDX-T05-20261025-F-SRC | 数量3 EACH，BOX，PRODUCTION，NORMAL，显式名称/autoNumber=false |
| 目标容器 | CDX-T05-20261025-F-DST | 数量1 EACH，BOX，PRODUCTION，NORMAL，显式名称/autoNumber=false |

总计6项创建并核实，0项已存在/跳过、0失败、0未尝试；两个Start各一次。Owner/StartReason先在已核对的既有上下文唯一选择，再于各新工单上下文复查；预先检查全部名称冲突，工单数量分别3与1且Start相等，不修改生产产品/工单/工艺。创建脚本默认只规划，`--apply`不可重放已有journal，任何不确定写入立即停止且不重试。见[计划](t05-fixtures/CDX-T05-20261025-F-plan.json)、[逐项创建记录](t05-fixtures/CDX-T05-20261025-F.json)、[执行日志](t05-fixtures/bulk-fixture-creation-output.txt)。

实际来源匹配与回显成功：`source_checks_passed=true`、`serialization_verified=true`，需求行原量2、实际量2、嵌套Actuals量2及顶层来源明细量2、来源/目标/产品版本均匹配。随后来源仍为3 EACH，目标QtyIssued为0、NetQtyRequired为2；ComponentIssue的execute=true次数为0。见[实际非执行预览及后读](t05-fixtures/bulk-live-preview-F.json)。来源/余额查询和ComponentIssueInquiry的execute=true是Query只读服务调用，不能算为投料。

首轮响应为`data`包装。初版未解包导致Reference is missing；检查内部同时发现需求行ActualQtyIssued被重算为0，而顶层来源明细为2。已修复解包，并明确补需求行嵌套Actuals以保留实际量，不将0放宽为通过。首轮[原始证据](t05-fixtures/bulk-live-preview-initial-F.json)保留；回归要求该初轮回显仍被拒绝，最终回显才通过。

## 重要边界与后续

前阶段只看Swagger时，IssueActuals无fromContainer，采用顶层IssueActualDetails暂存来源。后续读取实时`Shopfloor/api/$metadata`发现ActualsBulk继承IssueActuals，FromContainer为必填；IssueDetailsBulk继承IssueDetails。本机Designer只读快照与之相符，已有REST手册1-24～1-28说明子类型必须用`@odata.type`，Swagger不列出全部派生类型。故已改为派生类型单Actuals请求，消除双组实际量的不确定性。见[实时EDM](t05-fixtures/bulk-runtime-edm-types.json)、[Designer基类](t05-fixtures/bulk-designer-metadata.json)、[派生字段](t05-fixtures/bulk-designer-subtypes.json)、[派生回显](t05-fixtures/bulk-derived-verified-F.json)。

固定F实验验证派生请求后，后续已接入[模式2/5共用计划库及执行/核实/恢复](component-issue-shared-plans.md)，通过明确模式分派复用入口，不能把模式2请求伪装成模式5。当前仍不支持模式1/3/4、模式2部分量/补投、来源父子结构、第二数量、退料净额或替代料。真实故障/并发/现场版本及客户端联合验收继续保留，T05总验收仍未完成。

## 固定F单次执行与来源历史

脚本`scripts/probe_bulk_issue_execution.py`默认只规划，只允许已确认F批的环境、来源/目标/产品/需求行及单位ID。完整来源校验和派生类型回显后，要求目标实际历史为空、双方无本地unknown/in_flight投料占用。固定源/目标使用同一个永久journal和进程锁，即使复制输入fixture也不能绕过；`--apply`在请求前保存唯一Comments标记、body摘要及未知状态，只POST一次，不自动重试、重定向、改变规则或删除日志。它是受限实验，尚不提供通用双容器计划锁。

F本次仅提交一次HTTP200。观察来源Qty=1（此前3），需求QtyIssued=2/NetQtyRequired=0；唯一实际记录001b1b8000000002，来源00041080000000e7、目标00041080000000e8，数量2 EACH。见[执行前计划](t05-fixtures/bulk-derived-execution-plan-F.json)、[实际提交与后读](t05-fixtures/bulk-derived-execution-F.json)、[日志](t05-fixtures/bulk-derived-execution-output.txt)、[永久journal](t05-fixtures/CDX-T05-20261025-F-bulk-execution-probe.json)。

`tools/component_issue_bulk_receipts.py`从原始body摘要/环境/唯一标记重建受限请求，再只读核对HistoryMainline→IssueHistoryDetail→ComponentIssueHistory→IssueActualsHistory。F匹配事务400000000000f21b、主线00098880000001f2、需求明细0019be8000000007和批量实际历史001b1b8000000002；需求模式2、需求行/产品/单位/实际量及来源/目标均匹配，历史CDOTypeId=6939（IssueActualsHistoryBulk），不是模式5数量历史。四表核实complete/verified_success均true。来源历史字段由[实时列发现](t05-fixtures/bulk-history-schema.json)确认；[完整核实证据](t05-fixtures/bulk-derived-receipt-F.json)单独保存，不覆盖原未知结果journal。它证明本次事务曾提交，不证明后续来源余额不变。

现场再次调用脚本，在任何联网前被永久journal拒绝；原journal哈希不变、网络尝试0、投料提交0，见[拒绝证据](t05-fixtures/bulk-derived-replay-rejection-F.json)。本轮没有新MES对象或Start，不修改生产配置、不调用人工核实冒充自动证明。F来源仍有1但原实验永不重放。

派生执行阶段新增68项测试（固定实验25项、来源历史核实43项）；与既有模式2预览测试合跑130项通过。最终全量1084项测试、2项子测试通过，失败/错误/跳过均0；父pytest真实HTTPX网络尝试0，git diff --check通过。见[最新日志](t05-fixtures/bulk-execution-final-test-output.txt)、[JUnit](t05-fixtures/bulk-execution-final-junit.xml)、[网络](t05-fixtures/bulk-execution-final-network.json)、[机器汇总](t05-fixtures/bulk-execution-final-summary.json)。后文1016项为前阶段预览回归快照。

## 代码与验证

实现：tools/component_issue_bulk.py；注册/能力目录/按需加载/路由均已接入。准备脚本：scripts/prepare_bulk_issue_fixture.py。服务进程未重启，部署时需重启并刷新客户端工具列表。

新增68项测试覆盖来源身份/固定版本/单位/余额/父子关系、需求约束、缺字段/分页、回显篡改、真实包装/字符串数值/UseRor=false、严格MCP数量、只读注册/路由、6对象计数及不确定创建停机/journal防重放。专项68项通过，全量1016项及2项子测试通过，失败/错误/跳过均0，父pytest真实HTTPX网络尝试0，git diff --check通过。见[日志](t05-fixtures/bulk-final-test-output.txt)、[JUnit](t05-fixtures/bulk-final-junit.xml)、[网络检查](t05-fixtures/bulk-final-network.json)、[机器汇总](t05-fixtures/bulk-final-summary.json)。
