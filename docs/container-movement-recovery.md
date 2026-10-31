# MoveIn / MoveOut 参数与审批修复

2026-10-31。仅修改程序；本轮只读取了三类移动服务 Swagger 和本机原生元数据副本，没有发送 Shopfloor 执行/预览请求，没有对 SN_T_0029 进行过站，也没有更改层级、工序或吞吐配置。

## 已确认的问题与证据

1. `evaluate_mutations` 原来仅用累计工具调用次数判断审批，导致批准过写入并累计超过阈值后，单独的 `get_container_details` 也被要求确认。现在只有当前批次确实有该类写操作时才检查该类累计阈值；历史计数仍保留，新写入仍按原阈值和精确计划审批。legacy 混合批次中只读调用不随写入被拦截。
2. 原移动工具会将 `container_level_name` 序列化为 `container.level`。本部署实际请求曾拒绝 Level，尽管实时 Swagger 的 ContainerRef 仍声明该属性。修复后先读实际容器 ID，请求仅发送 name/instanceId；层级名称仅用于与现场层级核对，不自动丢弃或修改其含义。
3. MoveOut 实时 Swagger 未暴露顶层 `details`、`serviceDetails`、`transactionDetails`。本机厂商元数据副本显示其内部有 Move（MoveStd）、Thruput、ChangeQty 子事务，MoveOut 自身也没有 ServiceDetails 字段。由此不能把 `Field "Transaction Details" requires input` 直接归因于顶层 JSON 少字段；吞吐层级/对象、子容器及内部子事务的具体失败原因尚未现场核实。元数据副本不证明与现场部署完全一致。

精简契约清单与原始快照哈希见 [container-movement-contract.json](container-movement-contract.json)。完整实时抓取和本机元数据只读结果留在被 git 忽略的 tmp 下。

## 程序行为

`container_move`、`container_move_in`、`container_move_out` 保留原参数顺序，新增末尾可选参数 `container_instance_id`。工具执行前重新读取固定 Query 状态，并核实：

- 名称及实际16位容器 ID 一致，响应完整；重名应明确指定查询所得 ID。
- Status 明确为 Active，InProcess 为实际布尔值。MoveIn 要求 false，MoveOut 要求 true。
- 按真实 ContainerLevel 引用读取层级；明确指定的层级须一致，AllowMove 必须明确为 true。false 或未知时停止，不自动改配置。
- 写入体仅支持各移动变体的基础字段；不接受猜测的 Details/ServiceDetails，不允许 body_json 覆盖容器目标，也不能绕过数量和类型检查。false、空值、吞吐开关、签名或数据采集要求不作为绕过业务校验的尝试。

这些是客户端前检，不替代服务端的派工、资源、材料消耗、签名及其他动态业务校验；前检和提交之间也不是原子快照。新增前检可能使过去直接调用的脚本在读取不完整或层级未知时提前返回错误，这是有意限制。

两个 Agent 引擎共用 `core/movement_policy.py`：本轮某个容器移动失败或结果未知后，停止它的后续 MoveStd/MoveIn/MoveOut 写入，即使模型换了参数、路径或事务类型也不会再次调用移动函数；只读状态/历史核实仍可继续。被拦截的重试不再触发新的审批。原计划确认只对原参数有效，不能把通用“确认执行”用于另一个变更。

此停止规则保存在当前聊天请求状态中，不是跨请求的持久化事务计划或数据库锁。直接外部 MCP 调用也不经过 Agent 的本轮停止规则，但仍经过工具自身的参数及状态前检。超时未知结果后，后续请求仍须先读取原容器状态/历史，不能据此声称可安全重放。

共享模型规则明确：已进站不重复 MoveIn；保持用户要求的事务类型，不擅自改用 MoveStd；每站成功后另行回读实际位置及 InProcess，再决定下一动作。最后一站没有默认后继并不证明最终 MoveOut 成功，也不授权自动 Close。

## 验证与未完成范围

离线回归复现原对话中的只读重复审批、原参数确认与新参数再确认、失败后换参数/事务重试、Level 不入写入体、AllowMove=false、重复进站/未进站出站、数量绕过、跨移动变体字段误用及两个引擎的恢复行为。使用项目 venv 和 `scripts.offline_test_guard`；Windows 临时目录使用独立的新建 `--basetemp`，不清理已有临时目录。

本轮 168 项离线测试通过，覆盖上述移动回归及既有建模更新、工具路由/目录、审批、MCP、SSE 和 LangGraph 检查点测试；网络拦截插件未报告真实测试网络请求。

实际根因尚未通过原生失败日志及相关现场配置确认；没有声称已修复 MES 内部事务、完成真实 MoveOut、验证六站整条链路或实现新的完整路线批量执行器。代码及新工具定义需重启程序后生效，外部 MCP 客户端也需刷新定义。
