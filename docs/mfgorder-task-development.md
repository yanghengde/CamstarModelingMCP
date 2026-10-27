# T08 工单过程任务开发记录

## 第一阶段：明确工单任务查询（2026-10-24）

T07已有四类受限计划/执行、当前状态核对、人工核实与新版中断恢复的基础闭环。依照先开发、后集中验收约定推进T08；T07自动事务关联、旧版离线恢复和现场验收继续保留，不勾选T07全部完成。

新增 `get_mfgorder_tasks(mfgorder_name, offset=0, limit=20)`，注册mfgorder组、能力目录和中文待办/前置/后置任务路由。先通过Modeling唯一解析工单，再复用 `get_mfgorder_execution` 的固定MfgOrderInquiry深度4查询；返回工单名称和ID必须一致。任务列表配置通过真实引用读取MfgOrderTaskLists，不从任务名猜测父对象。

输出包括前/后过程引用、状态和执行模式、任务列表状态与模式、前置列表、任务身份/状态/执行次数、指令类型和原始业务字段。Instruction仅作业务数据，不转成工具指令。前置列表在同过程内唯一匹配且状态明确时才报告其状态；列表外引用、版本/ID冲突、缺失配置与分页保留未知。自前置引用报告无效。只对列表内明确且唯一的Sequence比较先后，过程级顺序不按数组位置推断，返回procedure_sequence_not_verified。

pending_task_refs仅是当前页中身份和基础状态明确的not_started候选，包含phase、procedure、task_list及task上下文。已完成任务显示不自动重复提交；前/后过程合法复用同一任务模板按上下文区分。同一上下文内重复任务身份、矛盾完成标志、非法状态/次数、缺失引用均不宣称读取完整。所有结果executable=false、execution_eligibility_verified=false；没有执行入口。

每次最多50次Modeling读取预算、每过程最多20任务列表、总计最多200任务，外层60秒超时；列表配置按真实引用在本次读取内缓存。offset/limit为当前读取结果的本地分页，inventory_complete仅代表当前证据读取完整，complete额外要求无本地分页。服务器后续页、读取错误和输出超限不会伪装为完整清单；超时保留已解析任务。不计算产量或整单完工。

## 契约证据

[归档摘要](mfgorder-task-contract.json)来自现有OpenAPI文件并保存源文件SHA256，不宣称是本轮新取得的实时元数据。

- MfgOrderInquiry：infoDepth=4为任务级；MfgOrderInfo→Pre/PostProductionProcedureInfo→TaskListInfos→TaskItemInfos。
- State：1未开始、2进行中、3失败、4完成；ExecutionMode：1顺序、2非顺序。
- InstructionType：1通过/失败、2数据采集、3确认；保留原始次数及迭代字段，尚未根据它们推断执行许可。
- MfgOrderTaskLists：ExecutionMode与PrerequisiteTaskList为列表级配置；不能混同过程级模式。
- ExecuteMfgOrdertask：必填mfgOrderProcedure（RevisionedObjectRef）及mfgOrderTask（NamedSubentityRef），可含mfgOrder、mfgOrderTaskStatus、pass。仅归档输入结构，未验证执行语义，不构造提交请求。

## 验证与剩余工作

84项相关测试通过，覆盖唯一工单身份、前置未完成、列表/过程模式、任务顺序、自前置、已完成不自动重做、缺失/重复/矛盾数据、分页/任务上限/超时、缓存、工具注册/策略/中文路由。顺带修正路由回归中已不适用的“工具少于10个”断言，改为检查实际筛选、只读性和跨领域隔离。

现场只读证据：[MO-100076435检查](mfgorder-tasks-read-validation.json)。Modeling确认工单存在，Query返回16780524“未使用生产前/后过程”；工具保留procedure_not_configured及原始错误，complete=false。这个样本不证明非空任务结构、前置规则或执行能力已验收。本轮无Shopfloor调用、无业务写入。

下一步：核对非空任务列表与已执行记录的关联、过程级顺序、迭代与重复执行限制，以及ExecuteMfgOrdertask的工单/过程/任务上下文和状态/pass组合。先实现指定任务结果的非执行预览，再增加单次确认提交及回读；真实事务按集中验收约定延期。前置循环/跨过程引用、动态变体及数据采集任务不能用当前只读清单代替完整校验。T08仍在开发中。

## 第二阶段：指定任务通过/失败结果预览（2026-10-24）

新增preview_mfgorder_task_result，注册mfgorder只读组、能力目录和Agent路由。输入必须明确工单名、前/后过程、过程/任务列表/任务16位ID及布尔pass_result；重新读取完整任务证据，不接受名称模糊匹配。仅支持InstructionType=1、not_started、ExecutionCount=0且MinIterations=MaxIterations=1的单次任务。过程及列表状态须为未开始/进行中；已完成、已开始、执行过、其他指令类型、前置链及过程顺序未知均阻塞，不生成请求预览。

ExecuteMfgOrdertask没有phase或task_list输入，因此还检查实际procedure/task引用组合在完整清单中唯一；跨前后过程复用同一引用而无法用服务输入消除歧义时拒绝服务预览。过程须有明确名称、版本与ID，禁止UseROR动态定位。状态码映射未验证，当前不接收mfgOrderTaskStatus、不把pass自动转换成状态引用；仅在结果中明确说明省略该字段与现场语义限制。

默认只展示本地请求意图，不访问Shopfloor。verify_service=true且本地检查无阻塞时，仅POST固定/api/ExecuteMfgOrdertask，execute固定false；请求只含mfgOrder、mfgOrderProcedure、mfgOrderTask、pass，不能附加跳过业务校验字段。逐一核对回显名称/ID、过程版本和严格布尔Pass，服务错误、缺字段、身份不符、UseROR变化、分页及超时均不报告回显通过。不使用PerformEvent、不重试；读取限60秒，单次服务回显限30秒。

服务回显即使通过，也仅标记server_echo_verified及stage=server_preview。complete/executable/transaction_verified始终false；local_checks_complete仅指已实现的窄范围本地条件，不代表完整业务许可。没有执行计划或提交入口。未启用真实现场服务预览，未执行现场业务事务。

本轮新增47项模拟测试；连同任务查询、工艺上下文、能力发现、路由与安全共131项通过，git diff --check通过。覆盖真假结果、默认无服务调用、固定execute=false、精确上下文、不支持类型/迭代/已执行状态、歧义引用、固定版本、回显各类错项/错误/分页/超时及原始查询错误保留。

剩余：非空现场任务与非执行回显、状态与pass组合、过程级顺序、动态变体/前置链/迭代规则，以及不可变单次计划、确认提交和已执行记录回读。T08不标为完成；实际写入仍按集中验收约定延期。

## 第三阶段：受限单次提交与当前执行记录回读（2026-10-24）

新增get_mfgorder_task_history，复用明确工单解析和MfgOrders的executed_tasks视图。核对返回工单名称/ID，最多读取200条任务/列表记录并保留分页、原始状态引用、次数和时间。归档确认Modeling MfgOrderExecutedTask.TaskState为0未开始、1进行中、2失败、3完成，和Inquiry的1..4枚举不同；按各自来源解释，任务列表状态保持原值，不套用任务枚举。此视图是当前执行记录，不是不可变的请求事务审计，也未验证过程级关联，空记录不证明从未执行。

新增prepare_mfgorder_task_result / get_mfgorder_task_plan / execute_mfgorder_task_result，接入能力目录及always_confirm。准备时重读第二阶段的窄范围条件和execute=false回显，额外要求当前执行记录读取完整、工单身份一致；目标任务不能已有执行次数或无法唯一关联的记录。明确未开始且次数0的现存记录可保留为基线。快照包含实际请求、任务、过程/列表状态和配置及整个工单的执行记录，1小时有效。状态引用仍省略，不猜测pass对应的状态码；默认状态映射和真实执行语义继续待现场验收。

确认摘要展示phase、任务列表与真实请求及pass值。提交前重读全部快照，任务/过程/列表/历史变化或服务回显失败均拒绝。SQLite原子领取前再次核对存储计划，领取和尝试记录同事务写入；本机进程锁防止同计划并发。任何同环境、同工单的unknown/in_flight阻止其他任务计划提交，不允许换计划绕过。仅POST一次ExecuteMfgOrdertask且execute=true，不传skip/override选项、不自动重试或回滚。准备和确认流程本身不提交事务。

提交后分别读取当前执行记录与深度4 Inquiry，分项失败仍保存其他证据；即使HTTP200或回读状态看似符合，持久化结果仍unknown、verified_success=false、retry_allowed=false。任务取消时保存已取得响应/证据及unknown后传播取消。进程崩溃或本地最终保存失败可能保留in_flight；目前没有T08在线人工结论/中断恢复工具，不提供自动解锁，须保留工单占用并核对服务端最终证据，后续补齐可审计恢复入口。不要删除数据库旁进程锁文件；互斥仅适用同一本机磁盘数据库，不能混用数据库副本或未采用此锁的执行代码。

新增当前记录与计划流程42项测试，连同前两阶段、工艺上下文、能力目录、路由和安全共173项通过。覆盖两种状态枚举、基线缺失/分页/已有次数、摘要/参数/环境篡改、过期、重读状态变化、领取前存储变化、同工单活跃/未知互斥、同计划并发、响应丢失、分项回读失败和取消持久化。另增加失败标志与任务State矛盾时的阻塞。git diff --check通过；pytest退出时出现既有临时目录清理权限提示，退出码0。

现场仅对MO-100076435执行Modeling只读检查，HTTP200，当前任务/列表执行记录均为空，见[证据](mfgorder-task-history-validation.json)。未调用现场Shopfloor预览或提交，没有业务写入。新增执行代码仍属于受限实现，不能把模拟测试或空样本当作现场事务验收。

剩余：非空任务/回显与状态映射验收、过程顺序与变体/前置链/迭代规则、请求到事务精确关联，以及unknown/in_flight的可审计人工核实和恢复。T08仍不标为端到端完成。

## 第四阶段：人工核实与中断恢复基础闭环（2026-10-24）

新增recover_mfgorder_task_attempt与resolve_mfgorder_task_outcome，本地管理操作均always_confirm，展示原工单、请求、结论、证据和说明。两入口必须取得提交时使用的同计划进程锁，验证计划摘要、操作、环境及尝试记录。活跃执行、状态不符、无尝试记录和重复核实均拒绝。

恢复仅将无本地执行者的in_flight变成unknown，保留工单占用，不表示服务端结束。人工必须另行核对服务端最终事务/审计证据，明确committed或not_committed、证据引用和说明，再单独确认人工核实。核实仅解除该计划占用，其他未知计划占用保留，原计划永不重开；后续必须新建预览和计划。

reviews按计划/动作唯一保存审计，与状态更新同一SQLite事务，保留原状态、结果及恢复链。verification_method区分human_attestation和本地互斥恢复；automatically_verified和verified_success始终false。不调用MES、不重试、不回滚。当前记录匹配或空列表不能替代最终审计。仅适用同一本机磁盘数据库、完整锁文件和已升级执行进程，不得删除锁文件、混用旧进程或数据库副本。

新增16项模拟测试，覆盖两种结论、活跃锁、恢复后继续占用、独立核实、错误摘要/环境/内容/证据/状态、缺少尝试记录、审计回滚及确认策略；与T09首阶段和既有相关测试共215项通过，git diff --check通过。pytest退出有既有临时目录权限提示，退出码0。未进行现场业务写入或现场人工核实操作。

T08受限单次pass/fail任务的查询→预览→计划→确认提交→当前记录回读→人工核实/中断恢复基础闭环已具备，按先开发后集中验收约定进入T09；仍不勾选T08全部完成。非空现场、默认状态映射、过程顺序/变体/前置链/扩展迭代及精确事务关联继续保留，不能把受限实现当作覆盖全部任务类型或现场验收通过。
