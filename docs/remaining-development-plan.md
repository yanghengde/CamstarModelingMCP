# 未完成项续接计划与本轮结果

2026-10-25数据采集续接：已补普通/分组定义、参数及组的增量维护和升版/删除计划、CollectData工序关联/解绑、布尔/带时区时间、分组采样预览与共用单次执行、分组历史及唯一标记历史核实，共20个collection工具。真实写入、非空历史与客户端验收仍未完成；对象引用提交及动态枚举/单位换算/自定义ParametricData仍未开放。见[完整工具链](collection-workflow-tools.md)。

更新：2026-10-25（Asia/Shanghai）。模式2正式单次计划已与模式5共用计划库、源/目标占用、执行/确认/核实/恢复入口。最新G批6个测试对象全部创建核实，2次Start；正式计划仅投料一次2 EACH，源3→1、目标已投2/净需求0，自动保存verified_committed，旧余额和已用计划均拒绝重放。此前F预览/固定实验报告保留为历史快照。

## 本轮已完成

| 项目 | 实现/现场结果 | 证据 |
|---|---|---|
| T06 未知结果恢复 | 新增人工最终证据核实与受本机进程锁保护的in_flight恢复；审计和状态原子保存，原计划不重开 | tools/collection_recovery.py；tests/test_collection_recovery.py |
| T06 提交完整性 | 提交前验证存储摘要/环境、重读参数绑定快照，领取前重查存储；原子写尝试记录，取消先持久化unknown再传播 | tools/collection_submission.py |
| T05 真实样本 | 2产品、1工单、1容器共4个，均创建并回读确认；真实物料需求2 EACH、模式5 | [创建日志](t05-fixtures/CDX-T05-20261024-B.json) |
| T05 查询修复 | 增加ParentContainer/ObjectName显式select，修复expand投影令根引用为空造成的unknown；拒绝布尔/非有限/负需求数量 | [真实需求](t05-fixtures/requirement-tool-validation.json) |
| T05 请求结构证据 | 固定execute=false检查serviceDetails + issueActualDetails，身份/行所属工单/产品版本/数量/单位回显通过；未执行投料 | [序列化证据](t05-fixtures/component-serialization-verified.json) |
| T09 文件下载代码 | 新增download_document_file，固定认证Download端点、明确版本、10MiB/60秒、无重定向、流式哈希/缓存、失败及取消清理，不访问Identifier | tools/document_files.py；tests/test_document_files.py |
| T05 数量意图校验 | 预览核对产品ID/固定版本、模式、需求行所属对象、单位、净剩余需求及第二数量；缺失为unknown、不匹配为blocked，序列校验仍保留unknown | [现场预览](t05-fixtures/quantity-preview-assessment.json)、tests/test_component_requirement_assessment.py |
| T05 业务拒绝规则 | 隔离容器标准执行探测返回13500677：数量不同须差异原因；预览已补数量差异规则，缺失规则不猜false，不为继续执行关闭校验 | [执行复核](t05-fixtures/execution-probe-review.json) |
| T04 可选周边汇总 | include_tasks/include_documents接入任务候选及文档元数据；明确文档要求才能判缺失，分页/失败/未检查范围保留未知 | [现场结果](t05-fixtures/readiness-with-tasks-documents.json) |
| T05 模式5受限执行 | 嵌套Actuals结构完成标准服务实验和正式工具验证：两组各2 EACH成功响应/非空实际历史；已实现准备/查询/确认提交/人工核实/中断恢复 | [工具说明](component-issue-plans.md)、[现场汇总](t05-fixtures/mode5-live-summary.json) |
| T05 唯一历史链核实 | 已实现reconcile_component_issue：唯一标记→主线→需求行/历史头→实际投料全部匹配才保存本地verified_committed；D已现场自动核实并解除该计划占用，无MES写入或再次投料 | [契约](t05-fixtures/issue-receipt-contract-D.json)、[实际核实及持久化](t05-fixtures/formal-reconciliation-D.json) |
| T05 首次部分投料 | 明确上下文差异原因查询/选择、两层回显与提交前重读、历史双原因ID校验已实现；E一次投入1 EACH、剩余1，四表核实为verified_committed | [原因与计划](t05-fixtures/partial-plan-snapshot-E.json)、[标准执行](t05-fixtures/partial-formal-execution-E.json)、[核实](t05-fixtures/partial-formal-reconciliation-E.json) |
| T05 模式5增量补投 | 平衡累计量/净需求/原需求，保留原需求且只写本次数量；E另用新计划补投1、累计2/剩余0，四表核实成功；旧余额计划被拒未发投料请求 | [补投汇总](t05-fixtures/topup-live-summary-E.json)、[执行](t05-fixtures/topup-formal-execution-E.json)、[核实](t05-fixtures/topup-formal-reconciliation-E.json)、[旧计划拒绝](t05-fixtures/topup-stale-rejection-E.json) |
| T05 故障与MCP客户端集成 | 增加真实子进程终止/崩溃、跨进程占用、响应丢失、历史晚到/歧义/权限拒绝、SQLite最终保存失败，以及独立stdio SDK和认证HTTP客户端完整流程；MES回复用归档样本回放 | [本轮报告](t05-fixtures/fault-acceptance-2026-10-25.md)、tests/test_component_issue_fault_integration.py |
| T05 MCP输入与离线环境 | 数量改为严格有限正数，阻止布尔/数字字符串自动转换；归档回放测试不再依赖本机.env地址，另测不同部署必须拒绝执行/核实，生产环境绑定未放宽 | tools/component_issue_plans.py；tests/test_component_issue_receipts.py；tests/test_nested_quantity_experiment.py |
| T05 模式2来源预览 | 新增来源/目标/需求/固定版本/单位/余额/根叶子校验及execute=false回显；F批6个对象全部核实，源3/需求2回显通过，后读源仍3/已投0/剩余2；无投料执行 | [说明](component-issue-bulk-preview.md)、[创建](t05-fixtures/CDX-T05-20261025-F.json)、[现场预览](t05-fixtures/bulk-live-preview-F.json) |
| T05 模式2派生执行实验 | 实时EDM确认IssueDetailsBulk/ActualsBulk，移除顶层重复明细；F一次2 EACH，源3→1、目标已投2/剩余0；唯一标记与模式2来源四表链核实；永久journal拒绝重放且未联网 | [派生回显](t05-fixtures/bulk-derived-verified-F.json)、[执行](t05-fixtures/bulk-derived-execution-F.json)、[历史核实](t05-fixtures/bulk-derived-receipt-F.json)、[拒绝](t05-fixtures/bulk-derived-replay-rejection-F.json) |
| T05 模式2正式计划闭环 | prepare_bulk_component_issue保存1小时不可变计划；同库按参与容器跨模式防重，源/目标重读及结构重建，复用执行/核实/人工/中断恢复；G正式一次2 EACH并verified_committed，旧计划/已用计划均拒绝 | [说明](component-issue-shared-plans.md)、[执行](t05-fixtures/shared-bulk-execution-G.json)、[核实](t05-fixtures/shared-bulk-reconciliation-G.json)、[汇总](t05-fixtures/shared-bulk-final-summary.json) |
| 回归 | 最新全量1131项测试和2项子测试通过，共用计划阶段新增47项；父pytest离线HTTPX真实网络尝试0，子进程独立阻断默认HTTPX传输；git diff --check通过，首轮目录文案断言失败及此前报告保留 | [最新日志](t05-fixtures/shared-bulk-final-test-output.txt)、[JUnit](t05-fixtures/shared-bulk-final-junit.xml)、[网络](t05-fixtures/shared-bulk-final-network.json)、[汇总](t05-fixtures/shared-bulk-final-summary.json) |

## 已创建测试对象与失败记录

成功批次CDX-T05-20261024-B：

| 类型 | 名称 | 关键上下文 |
|---|---|---|
| 物料产品 | CDX-T05-20261024-B-COMP / 1 | ProductType BOX，StdStartUOM EACH |
| 目标产品 | CDX-T05-20261024-B-TARGET / 1 | Workflow WF-WM-900A / A，StdStartUOM EACH |
| 工单 | CDX-T05-20261024-B-MO | 目标数量1，Released，物料行T05-QTY要求2 EACH，issueControl=5 |
| 容器 | CDX-T05-20261024-B-C01 | Qty=1，Level BOX，Owner PRODUCTION，StartReason NORMAL，明确名称、autoNumber=false |

本批4/4创建并核实，无跳过/最终失败/未尝试项；Start只调用一次。Owner/StartReason两轮RequestSelectionValues均只有一个有效值；Container名称先做全局冲突检查，未使用全局编号规则推断。

初始批次A计划4项，第一项产品CDX-T05-20261024-A-COMP被HTTP400拒绝，0项回读确认创建、1项失败、3项未尝试。缓存Product Swagger的uom属性在实时ProductEntity不存在；实时元数据只有StdStartUOM，修正后新批次B成功。B的首个工单请求曾因IsPhantom只读被拒，随后确认名称仍不存在，保留原错误，以仅必要字段受控续接。工单创建成功后，expand响应把未选择的根Product/Qty投影为null；已用普通读取核对根对象、展开读取核对物料行，修复验证而没有重发已成功的工单创建。所有原始响应/受控续接均留档，不删除或改名掩盖失败。

T09批次A计划3项（文档、文档集、独立Spec），CreateDocuments首项返回HTTP400“Upload folder doesn't exist”。0项创建、1项服务器拒绝、2项未尝试；随后只读核对三个实体前缀均无记录。另按REST手册2-23检查独立Upload端点一次，同样返回HTTP400缺少上传目录，见[响应](t09-fixtures/upload-endpoint-check.json)。没有重放创建请求，也没有在本机创建目录冒充修复远端服务。源测试txt只在本地docs/t09-fixtures保存。

此前续接新增C/D两个隔离批次，各2产品、1工单、1容器，共8/8创建并逐项核实，0失败、0跳过、0未尝试。均为BOX层级、产品版本1、目标容器Qty1、EACH、PRODUCTION/NORMAL、明确名称；两轮Start选择值均唯一，所有名称预先查冲突，每组Start一次。C用于嵌套Actuals契约实验，D用于正式计划工具；各标准投料请求一次，实际QtyIssued均2、NetQtyRequired均0，并有实际投料历史。原生产对象未改动，B的失败journal和全部对象保留。D当时先保留unknown；本轮只读四表核实后，计划9996952fc34849bb92c08ff5ea5d0bd2已转verified_committed，自动核实方法/原结果/证据全部保存。没有调用人工核实工具冒充human_attestation。

前次自动核实阶段没有创建MES对象或提交MES业务事务，只查询隔离D的历史并更新本地计划/审计。原计划不可重用，B失败请求及C实验journal保留原状。

后续首次部分投料开发新增E批：独立差异原因CDX-T05-20261024-E-PARTIAL，加2产品/1工单/1容器，共5/5创建并回读确认；0失败、0跳过、0未尝试。总数5先核对低于SAFE_CREATE_THRESHOLD，默认Replace Component原因未套用，现有原因/生产对象未修改。容器CDX-T05-20261024-E-C01，Qty1，BOX，显式命名/autoNumber=false，工单CDX-T05-20261024-E-MO，产品CDX-T05-20261024-E-TARGET / 1，Owner PRODUCTION、StartReason NORMAL均经两轮唯一选择核对。需求产品E-COMP / 1，要求2 EACH；首次仅投入1 EACH、明确测试差异原因，一次execute=true成功，已投量1/净需求1，两层历史原因均0019c8800000001b。计划a020dd91fbd04188ac15bdfa75e5506b已自动核实为verified_committed，原计划不可重用；没有补投剩余1。

上述为首次部分量阶段快照。本轮增量补投开发没有创建MES对象，使用E既有样本另建新计划33b2963a7af548e28aac496fc9c82613，在累计1/净剩余1的基线上只补投1 EACH，标准请求一次HTTP200，累计2/剩余0，新事务四表核实为verified_committed。原首次计划及历史保留，未重放旧请求。另一份相同旧基线计划在余额变0后返回submitted=false，未调用投料端点；其本地ready记录保留，但该基线已失效且不能通过提交前复核。退料/移除历史非空或缺失、待处理实际量非0、余额不一致与超净剩余均拒绝。

## 必须继续的开发/部署任务

最新G批6/6创建核实，0已存在/跳过、0失败、0未尝试：CDX-T05-20261025-G-COMP/1、G-TARGET/1、G-SRC-MO（Qty3）、G-DST-MO（Qty1，模式2需求2 EACH）、G-SRC（Qty3）、G-DST（Qty1），其余名称均同前缀CDX-T05-20261025。两个Start各一次，BOX、PRODUCTION/NORMAL、显式命名/autoNumber=false；Owner/StartReason预检与新上下文均唯一，名称预查无冲突。G正式计划8e1e08ebf90c46f78e97113276584df3仅执行一次2 EACH，事务400000000000f226及6939实际历史001b1b8000000003已自动核实并持久化；原unknown结果/attempt/审计保留。源1、目标已投2/剩余0；两份旧/已用计划拒绝，无额外投料。F原实验及对象未重放或修改。见[完整说明](component-issue-shared-plans.md)。

最新模式2新增F批6/6创建核实，0已存在/跳过、0失败、0未尝试：CDX-T05-20261025-F-COMP/1、F-TARGET/1、F-SRC-MO（Qty3）、F-DST-MO（Qty1，T05-BULK模式2需求2 EACH）、F-SRC（Qty3）、F-DST（Qty1），其余名称也使用同一CDX-T05-20261025前缀。两个Start各一次，BOX、PRODUCTION/NORMAL、显式名称/autoNumber=false；选择值在预检/新上下文均唯一，名称预查无冲突。两轮ComponentIssue均execute=false；初轮实际量重算0的证据保留，最终补嵌套Actuals后需求行/嵌套量/来源明细均2且引用匹配，后读来源仍3、目标已投0/净剩余2。无模式2执行计划。详见[模式2说明](component-issue-bulk-preview.md)。

| 优先级 | 编号 | 剩余工作 | 当前证据/阻塞 | 可验收结果 |
|---|---|---|---|---|
| P1 | T05-RECEIPT-OPS | 模式5首次/部分/增量补投与四表核实已实现；隔离进程故障和stdio/认证HTTP客户端集成已补，继续真实MES响应丢失/并发、其他版本及聊天端联合验收 | 本机真实进程退出/锁释放/跨进程占用已测试，MES回复和晚到历史仍是回放；旧余额计划拒绝此前已现场实测。当前配置地址与历史样本不同，不复用旧部署计划执行 | 真实故障后仍唯一关联；不完整/歧义继续unknown，旧计划不重放；人工核实与自动核实标记准确 |
| P0 | T09-UPLOAD-ENV | 在部署主机核对REST上传目录配置，创建实际目录并给运行服务账号所需读写权限 | [创建响应](t09-fixtures/CDX-T09-20261024-A.json)明确缺少上传文件夹；当前工具没有远程文件系统配置接口 | CreateDocuments单文件200、模型唯一可见；之后再创建文档集/Spec、下载哈希与源文件一致 |
| P1 | T05-TRACKING | 模式2通用单次计划/双占用/跨模式防重/确认核实恢复及G现场闭环已完成；继续模式1/3/4独立契约、模式2部分量/补投/更复杂来源及业务边界 | 模式2仅首次完整需求、单个根叶子来源、无第二数量/退料/替代料；真实进程故障与争用已在回放中验证，MES冻结/过期/权限/其他版本及真实故障尚待验收 | 各模式独立请求/来源余额/历史链/确认和异常验收，不转换追踪模式、不关闭校验；全写进程统一升级后本地防重才成立 |
| P1 | T05-REMAINING | 退料/移除后净额、替代料、第二数量、标准BOM需求，以及现场IssuePoint/序列非空规则 | 已有消耗但余额一致、无退料/待处理量的模式5补投已实测；其他净额/来源语义不猜测 | 每类需求与净额规则、执行拒绝及成功历史独立验收，不关闭校验、不覆盖累计历史 |
| P1 | T06-TYPES | 验证布尔、时间戳、对象及分组采样的实际序列化/执行/历史格式，再扩展支持 | 目前仍仅数值/字符串提交，其他类型不猜转换 | 明确类型的非空预览及真实提交/回读；时区/对象引用/分组完整性可核对 |
| P1 | T06/T07/T08-TXN | 精确请求→事务关联；未知结果自动核实需可靠证据 | 当前相同值/当前关系/空历史不足以证明请求因果；人工结论不伪装自动验证 | 正常/超时/并发事务仍能唯一定位本请求，否则保留unknown |
| P1 | T07-RULES | 非空容量、层级、单位、第二数量、Split Execute语义与服务拒绝案例 | 受限代码与模拟测试已具备，不是全部现场规则通过 | 四类操作独立的成功/拒绝/部分失败证据，不自动回滚 |
| P1 | T08-RULES | 非空前后过程任务、默认状态映射、顺序/前置链、变体/迭代 | 原工单无前后过程；新恢复入口为受限基础闭环 | 明确业务上下文/状态及重复限制，不把任务完成当作产量报工 |
| P1 | T09-ACCESS | 上传目录修复后重做非空文档链、正常账号下载、无凭证/无权限访问测试及客户端预览 | 正式Download路由有文档依据；当前认证访问缺失文档返回400，无凭证401。无正例文件，不能报现场下载通过 | 当前账号HTTP200、真实响应URL/文件头、大小/哈希验证；其他权限失败不误报可用 |
| P2 | T04-CLIENT | 可选任务/文档摘要已接入，补非空配置和聊天端展示验收 | 隔离工单现场汇总3项通过/7项未知，未配置工单过程；文档要求未指定，不判缺失 | 展示来源、分页和未检查范围；明确业务要求传递正确，不执行文档内容或认定允许开工 |

配置修复后，用新的隔离前缀重跑文档准备脚本；不要删除旧journal或重放不确定请求。T05/T09脚本默认只规划，只有--apply才创建。原有生产产品/工单/工艺未修改，创建的测试对象保留用于后续开发，不做自动清理。
