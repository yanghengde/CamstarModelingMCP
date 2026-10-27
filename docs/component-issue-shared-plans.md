# 模式2与模式5共用单次投料计划

日期：2026-10-25（Asia/Shanghai）。模式2已接入正式计划库、确认展示、单次执行、自动历史核实、人工最终核实及进程中断恢复。模式2仍只支持单来源、首次完整需求、固定产品版本/同单位、根叶子来源，无退料/第二数量/替代料/部分量或补投。模式5既有完整量/部分量/补投保持原范围。

## 使用流程

1. 模式2调用`prepare_bulk_component_issue(container_name, source_container_name, product_name, product_revision, qty, container_instance_id?, source_container_instance_id?)`。工具只读MES并保存本地1小时计划；强制来源校验和派生类型非执行回显。名称/固定版本必填，数量严格为有限正数，拒绝布尔/数字字符串。
2. 展示返回的计划ID/摘要、源与目标、来源观察余额/本次数量/预测剩余、目标需求及完整请求。计划准备不预留MES库存，也不占用参与容器。
3. 用户确认后调用共用`issue_components(plan_id, plan_digest)`。模式2重读源/目标状态、来源余额/父子关系、目标需求及派生回显，摘要须与原计划一致；随后在同SQLite事务中重新检查存储/期限，检查源/目标与所有unknown/in_flight计划参与容器是否相交，原子保存in_flight及attempt才单次提交。HTTP200、错误、超时或取消均先保留unknown；不自动重试。
4. `get_component_issue_plan`读取原请求与结果。`reconcile_component_issue`根据计划模式分派：模式5用既有数量链，模式2要求四表链中的6939批量来源历史、原源/目标、产品、单位、需求行、上下文及数量全部匹配。核实与审计同SQLite事务保存verified_committed，只解除本计划占用，原计划不可重用。
5. 进程退出留下in_flight时，经确认调用`recover_component_issue_attempt`：取得原计划OS锁且有attempt才转unknown，双方占用保留，不证明服务端结束。人工检查最终服务端事务证据后才能用`resolve_component_issue_outcome`保存明确人工结论；人工结果与自动核实的标记不同。

上述执行/核实/恢复/人工结论都沿用always_confirm策略，Agent确认预览会按计划类型显示来源扣减。直接MCP客户端必须提供同等确认；测试程序内的授权调用不代替聊天界面的人机确认验收。`preview_bulk_component_issue`继续只是只读预览，不直接执行其输出。

## 存储、摘要与占用

继续使用`data/component_issue_plans.sqlite3`及原plans/attempts/reviews/receipt_checks表，没有迁移旧计划或重写摘要。旧模式5快照无issue_control标记时仍明确解释为模式5；新模式2快照显式标记2。两类计划均从约束重建原请求，拒绝额外实际量、来源遗漏、模式切换及覆盖校验字段；重算摘要不能绕过业务结构核对。

模式2快照绑定目标context、来源source_context、来源原始余额列、需求约束、派生请求、唯一标记和重读参数。双方工序/产品/数量/单位/状态/活动时间等被保留；Inquiry临时InstanceID不参与摘要，回显中的临时服务实例不存入摘要。来源数量、关系、需求或上下文变化时，旧计划在提交前被拒绝。

占用按请求中所有来源/目标ID计算，与同环境所有unknown/in_flight计划比较。因此同源不同目标、源作另一计划目标、目标作另一计划来源以及模式5写入模式2来源均受保护；反向也成立。SQLite BEGIN IMMEDIATE保证跨进程领取不会同时成功，单计划OS锁防止执行/恢复/核实抢占。核实不完整、审计插入失败或核实期间存储/attempt变化均不解除占用。

这仅协调同机同计划库、已升级的投料执行进程。**部署须统一重启并升级所有使用该计划库的写入进程**；旧版只识别目标，不能声称兼容来源占用。它不是MES库存预留或分布式锁，不协调其他数据库、其他业务工具或外部客户端。MES仍执行最终现场校验；不能据此保证读取与提交之间没有外部状态变化。

## G批正式工具现场验收

为避免重放F固定实验，按Container Start技能准备G批6个隔离对象，低于阈值20，全部创建并逐项核实：

| 类型 | 名称 | 内容 |
|---|---|---|
| 产品 | CDX-T05-20261025-G-COMP / 1 | 来源物料，EACH，WF-WM-900A/A |
| 产品 | CDX-T05-20261025-G-TARGET / 1 | 目标产品，EACH，WF-WM-900A/A |
| 工单 | CDX-T05-20261025-G-SRC-MO | 来源产品，数量3 |
| 工单 | CDX-T05-20261025-G-DST-MO | 目标产品，数量1；模式2需求2 EACH |
| 来源容器 | CDX-T05-20261025-G-SRC | 3 EACH，BOX，PRODUCTION，NORMAL |
| 目标容器 | CDX-T05-20261025-G-DST | 数量1，BOX，PRODUCTION，NORMAL |

两个容器均显式命名/autoNumber=false，Owner/StartReason在预检及新上下文唯一；名称全局预查无冲突，Start分别3/1且不超对应工单数量。共6创建核实，0已存在/跳过、0失败、0未尝试；两个Start各一次。未修改生产产品/工单/工艺，F实验及journal保留。

见[准备计划](t05-fixtures/CDX-T05-20261025-G-plan.json)、[逐项创建](t05-fixtures/CDX-T05-20261025-G.json)、[创建日志](t05-fixtures/shared-bulk-fixture-creation-output.txt)。

G生成正式模式2计划和同基线另一份计划，确认内容展示源3/本次2/预测源1及目标需求2。正式计划仅execute=true一次HTTP200，来源回读1，目标QtyIssued=2、NetQtyRequired=0。共用reconcile_component_issue自动保存verified_committed，原unknown结果、attempt及来源四表证据同事务保留；未调用人工核实冒充自动验证。另一份旧余额计划submitted=false，已用计划submitted=false，没有再次发出投料。

证据：[正式计划](t05-fixtures/shared-bulk-plan-G.json)、[不可变快照](t05-fixtures/shared-bulk-snapshot-G.json)、[确认内容](t05-fixtures/shared-bulk-confirmation-G.txt)、[执行/后读](t05-fixtures/shared-bulk-execution-G.json)、[核实及持久化](t05-fixtures/shared-bulk-reconciliation-G.json)、[旧计划拒绝](t05-fixtures/shared-bulk-stale-rejection-G.json)、[已用计划拒绝](t05-fixtures/shared-bulk-replay-rejection-G.json)。旧余额计划的ready记录保留，但重读不能通过，不表示可执行。

## 测试与边界

新增47项测试覆盖：计划摘要/原请求重建/确认、双向跨模式和角色交叉占用、同源多计划、源/目标/需求变化、重算摘要篡改拒绝、旧模式5快照兼容、过期/取消/并发、claim及核实审计回滚、最终保存失败、晚到历史、人工/恢复状态区分、attempt变化及严格MCP数量。

其中包含真实子进程强制终止后来源继续占用、两个真实进程争用同源仅一条提交，以及独立stdio SDK客户端的准备→执行→核实→拒绝重放。MES响应仍由归档F数据和固定MockTransport提供，默认HTTPX网络被阻断；不能把这些故障回放说成真实MES故障已验收。原模式5故障/客户端测试也继续运行。

首轮全量1130项、2项子测试通过，1项能力目录测试因说明漏写“不扣库存”失败；已补回只读限制文字，首轮[日志](t05-fixtures/shared-bulk-initial-test-output.txt)、[JUnit](t05-fixtures/shared-bulk-initial-junit.xml)和[网络记录](t05-fixtures/shared-bulk-initial-network.json)保留。最终结果见[日志](t05-fixtures/shared-bulk-final-test-output.txt)、[JUnit](t05-fixtures/shared-bulk-final-junit.xml)、[网络](t05-fixtures/shared-bulk-final-network.json)。

最终全量1131项测试、2项子测试通过，失败/错误/跳过均0，父pytest真实HTTPX网络尝试0，git diff --check通过。G正式事务400000000000f226，实际历史001b1b8000000003，计划已verified_committed。见[机器汇总](t05-fixtures/shared-bulk-final-summary.json)。

仍待：真实MES故障/并发与聊天端确认取消联合验收、其他部署版本/权限/冻结等业务边界；模式2部分量/补投、第二数量、退料净额、替代料；模式1/3/4及T09上传目录。T05总完成不勾选。当前运行服务未重启，新工具及双容器占用逻辑需统一升级后加载。
