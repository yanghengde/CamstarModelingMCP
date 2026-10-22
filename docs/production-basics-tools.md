# 已接入的生产基础工具

2026-10-22。仅接入契约和基本语义已核实的接口；不新增 Agent 编排。

| MCP 工具 | 产品接口 | 参数说明 |
|---|---|---|
| list_production_reasons | GET Modeling /api/{已核实原因码实体} | kind：hold/release/scrap/quantity_adjust/rework/container_defect；top 1–100，skip≥0，可选 filter_expr |
| hold_container | POST Shopfloor /api/Hold | container_name、reason_name；可选 container_instance_id、comments |
| release_container | POST Shopfloor /api/Release | 同上；仅容器解冻，不是工单释放 |
| change_container_quantity | POST Shopfloor /api/ChangeQty | container_name、change_qty_type、reason_name、qty；可选 container_instance_id、qty2、comments |

数量类型严格使用产品枚举：1 defect、2 loss、3 sell、4 adjust、5 bonus、6 buy。qty 是事务数量，不是修改后的余额；不推断其与报废、良率或工单完工的关系。符号及原因适用性由产品业务规则校验。封装只支持单条数量变更明细，不开放任意 JSON 覆盖。

Hold/Release 的 Swagger required 仅列 container；本封装额外要求明确原因名。这是封装约束，不伪称产品统一必填规则。同名容器应提供真实 instance ID。

三个写入工具登记为 update、always_confirm，沿用现有应用审批策略；直接使用 MCP 的客户端仍应自行实施审批，目录声明不等于独立 MCP 权限防火墙。封装不自动重试失败写入，不推断成功或自动回滚。

## 验证

- 27 项单元与回归测试通过：新工具请求契约、枚举、无效参数拦截、错误透传及既有质量/移站工具。
- 通过 mcp.list_tools 确认四项工具实际注册。
- 六类原因码均实际 GET 返回 HTTP 200，且包含 value 集合。ScrapReasons 和 ContainerDefectReasons 本次返回空列表；其他四类至少有一条。空列表不虚构默认原因码。
- 未执行冻结、解冻或数量变更写入。尚不能声称实际状态流转或所有业务组合已通过现场验收。
- 结果记录：production-basics-validation.json。

## 本轮未接入

MoveWithThruput、CollectData 尚需进一步验证参数组合及现场语义；ExecuteMfgOrdertask 尚需任务引用及执行状态规则验证。独立工单下发、整单报工、生产容器报废均不新增。既有 MoveIn/MoveOut、ContainerDefect、Rework、Start 保持现有能力。

部署后重启 MCP 服务并让客户端刷新工具清单。
