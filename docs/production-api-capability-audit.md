# 生产基础能力 API 事实盘点

核对日期：2026-10-22。依据当前配置的 Modeling、Shopfloor、Query 服务实时 Swagger，以及 tools/ 下的 MCP 实现和注册清单。本次仅 GET 接口定义，未执行业务写入；Swagger HTTP 200 证明契约可读，不代表事务权限、现场状态或业务流程已验证。

原始证据：production-api-audit-evidence.json。每项保存服务名、Swagger 相对地址、方法、请求体引用及关键 schema。完整下载件位于 tmp/<服务>-<名称>.json。

## 1. 生产异常处理

| 产品接口 | 关键契约事实 | MCP现状 | 结论 |
|---|---|---|---|
| Shopfloor POST /api/Hold | Hold schema 必填 container，含 holdReason | 未有专用工具 | 可接入容器冻结；原因码及现场必填规则仍须验证 |
| Shopfloor POST /api/Release | Release schema 必填 container，含 releaseReason | 未有专用工具 | 可接入容器解冻；不是工单释放 |
| Shopfloor POST /api/ContainerDefect | 必填 container、serviceDetails | 已有 container_defect | 已具备缺陷登记封装，不等于完整异常处置闭环 |
| Shopfloor POST /api/Rework | 原生返工事务 | 已有 rework | 已封装，需结合原因码、路线与现场状态验收 |
| Shopfloor POST /api/ChangeQty | 必填 container、serviceDetails；ChangeQtyDetails 含 changeQtyType、qty、qty2、reasonCode | 未有专用工具 | 可封装数量变更；不能简单命名为 scrap |
| Modeling 原因码实体 | HoldReasons、ReleaseReasons、ScrapReasons、QtyAdjustReasons、ReworkReasons、ContainerDefectReasons Swagger已读取 | 未有对应专用领域工具 | 优先补查询与选值，避免模型猜原因码 |

ChangeQtyDetails.changeQtyType 产品注释明确：1=defect、2=loss、3=sell、4=adjust、5=bonus、6=buy。不能自行添加 scrap 枚举或认定某种类型必然等价于本厂报废流程。

PartScrap 虽存在，但 schema 必填 resource、scrapQty，包含 resourceParts、PartTxnServiceDetail；属于资源零件相关事务证据，不应当作生产容器报废接口接入。

## 2. 工单报工

| 产品接口 | 已确认事实 | MCP现状 | 边界 |
|---|---|---|---|
| Shopfloor POST /api/MoveIn | 原生进站事务 | container_move_in | 进站不是产量报工 |
| Shopfloor POST /api/MoveOut | schema 含 container、qty、qty2、thruputAllQty、resource 等 | container_move_out | 可作为工序出站相关基础能力，是否满足业务报工要验证统计和历史 |
| Shopfloor POST /api/MoveWithThruput | schema 含 container、thruputAllQty 等；其注释说明覆盖数量为最大允许量 | 未有专用工具 | 可列入吞吐量/移站专项验证，不承诺完整工单报工 |
| Shopfloor POST /api/CollectData | schema 必填 container | 未有专用工具 | 可作为数据采集候选，需继续核对采集定义及明细 |
| Shopfloor POST /api/ExecuteMfgOrdertask | 必填 mfgOrderProcedure、mfgOrderTask；含 mfgOrderTaskStatus、pass | 未有专用工具 | 是工单过程任务执行，不能混同生产数量报工 |
| Query /api/MfgOrderInquiry | 存在查询契约 | get_mfgorder_execution | 现有工具用于前后置过程任务证据，不代表实际产量 |

未确认独立的“良品+不良品+工时+整单完工”统一事务，也未验证产品原生幂等键。因此不能承诺一条接口解决完整报工或重复请求自动去重。

## 3. 工单下发

- Modeling /api/MfgOrders 有 GET、POST；/api/MfgOrders/{key} 有 GET、PUT、DELETE。已有 list/create/update/get 等工具。
- MfgOrderEntity 真实字段包括 product、qty、qty2、orderStatus、releaseDate、计划起止日期、前后置过程等。写入 orderStatus/releaseDate 是实体维护，不能仅凭字段名就认为执行了带前置校验和联动的“下发”事务。
- 当前 PATCH 工具已禁用：代码明确提示实时 Swagger 未验证 PATCH。不能列为可用能力。
- Shopfloor /api/Start 已有 container_start，可创建生产容器；创建容器不能替代工单释放，也不证明工单已派到设备。
- Shopfloor /api/MfgOrderTxn 确实存在，但其 schema 无 orderStatus 字段，不能据名称将其封装为 release_mfgorder。
- 未在本次目录检索和契约核对中确认独立 ReleaseMfgOrder/DispatchMfgOrder 事务；这不是宣称产品所有版本都没有该能力。

## 4. 推荐接入顺序

1. 原因码读取 + Hold/Release：产品语义明确，先补异常冻结/解冻基础闭环。
2. ChangeQty：按产品枚举逐类接入；优先在测试容器验证数量、原因、历史和恢复限制。
3. MoveOut现有参数覆盖盘点 + MoveWithThruput：核实出站、吞吐量历史与工单汇总的对应关系后，再定义“报工”业务接口。
4. ExecuteMfgOrdertask：独立作为工单过程任务能力，保持与生产报工区分。
5. 工单下发：先核对当前系统实际操作页面及其调用契约；若仅是订单状态维护，就明确按状态维护命名，不创造不存在的下发事务。

候选接口开发前仍需验证：实际必填字段、引用解析、权限、状态限制、成功响应、失败响应、历史回读及重复提交行为。Swagger required 不是全部业务必填规则。本次盘点未新增MCP工具、未改动产品业务数据。
