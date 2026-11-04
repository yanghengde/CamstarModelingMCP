# 事件处理日志追加

2026-11-04。新增 `get_quality_event_log_context` 和 `prepare_quality_event_log_append`，使用原生 UpdateEventLogs（8761）及统一质量计划执行器。

## 业务范围

入口为已提交、Active、未冻结的 Category1 基础 Event，已有标准8085 EventData，当前员工为原责任人且组织角色授权有效。沿用扩展资料的复杂关联检查；流程、审批、检查表、法规报告、NCR、现场自定义数据、客户或其它复杂明细仍不支持。新增一条文字处理日志，可明确选择已有类别，最多4000 UTF-16字符。原日志最多99条，为追加后的完整100条证据读取留出空间。

先读取原事件及完整日志，再明确 `log_text` 和本次操作说明 `comments`，准备静态计划。确认后的 `execute_quality_transaction` 复读业务状态、原日志、契约和当前会话授权，只提交一次。新日志的作者及日期由原生当前用户和系统时间表达式生成，不接受作者、日期、原日志ID或通用明细参数。

## 原记录保护

标准 `EventDataDetails_ModifyLogs` 使用 UpdateSubentityList，目标为 EventData.EventLogs，来源为 EventLogDetails。因此请求包含完整原清单及一条新增记录，不能只传新增记录。旧项逐条核对SQL和固定Load的内容、原ID、作者、类别、Local/GMT时间，保持原值；缺作者/日期时拒绝，以免原生表达式补成当前值。重复、遗漏、未知扩展、超过上限均拒绝。

标准 UpdateEventDetails_PreValidate 设置 Transaction::IsExecute=true，准备阶段只使用固定Load，不调用普通POST作为预览。旧日志逐条Execute可能形成新的本次历史明细；代码不将该副作用称作历史不变。现有历史不可删除或改写，真实历史效果仍须现场验收。

## 结果核实

HTTP成功或失败、超时都保留unknown和对象占用，禁止自动重试。inspect读取原Event/原EventData关联，检查旧日志丢失或改写，并列出新增记录。另读取共享UpdateEventDetailsHistory表内8754类型、8761事务及本次标记匹配的有限历史候选。候选及当前同文记录均不自动归因成功，仍需最终事务证据人工review。

## 证据与验收状态

标准模型只读核对涵盖 EventLog（8367）、EventLogDetail（8371）、UpdateEventLogsHistory（8754）、三个字段映射、Load/ModifyLogs/预校验及历史逻辑；本地证据位于 `docs/quality-fixtures/event-log-native-*.json`。标准MDB SHA256 为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。

11月2日读取现场Swagger成功，实际模型名为 UpdateEventLogsShopfloorPerformEventModel，测试使用脱离网络的契约夹具。11月4日重新核对未取得有效JSON契约，尚未完成真实对象Load、业务提交、原生作者/日期、旧项保存和最终历史验收。本轮没有MES业务写入、部署或服务重启。测试报告另记于统一交付清单，不以测试通过替代现场验收。


## 明确类别追加（第二轮）

复用两个已有入口。`comment_type_name` 为明确类别名称，可附 `comment_type_instance_id` 核对原ID。名称省略时沿用无类别追加。单独传ID、空名称、重名、冻结、未知派生类型或版本不一致会拒绝准备。

标准CommentType的CDO类型为8414、持久表为CommentType。选定类别使用已有配置读取逻辑，核对建模名称、ID、冻结状态与SQL版本，另核对标准类型。在准备期间复读，在执行重建快照时再次核对，并将类别名称/ID加入配置读取资源。旧日志的类别保持原值。新记录的类别引用由已核实快照生成。

inspect增加新增记录正文、类别和作者的匹配ID，匹配结果作为待审核证据。事务历史候选与最终人工审计继续按原流程处理。

本轮相关回归440项通过，其中日志专项39项，离线HTTPX真实传输尝试0。覆盖MCP类别读取到计划、无类别兼容、旧类别保持、类别冻结/重名/错ID/错类型、版本变化、执行前失效和新记录类别不符。现场只读列契约核对未取得有效JSON，真实类别保存及最终历史仍待验收。本地报告为 `docs/quality-fixtures/event-log-types-junit.xml` 和 `event-log-types-network.json`。
