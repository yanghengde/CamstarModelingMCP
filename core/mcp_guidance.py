"""Guidance delivered to external clients during MCP initialization."""

MCP_INSTRUCTIONS = """Camstar Modeling, Shopfloor and Query MCP.
只读查询优先规则（适用于WorkBuddy等外部客户端，不仅是本地Agent）：
1. 已知工单名的开工/计划/完工日期，直接run_camstar_read_template(template="order_dates",
parameters={"order_names":["MO-..."],"timezone":"Asia/Shanghai"})。参数schema已完整提供，
不必先查表结构、SQL或元数据；模板同时覆盖关闭容器、GMT原值、计划日期和实际首箱日期。
2. 其它批量/跨表查询优先get_camstar_read_metadata(kind="overview")后execute_camstar_read_plan；
工具名不明确用search_camstar_read_tools，单对象已有专用工具直接调用。
3. 当前在制不等于InProcess=true。按明确工单/容器持久ID关联，分别保留Status、
ProductionComplete、InProcess与单位；有分页时不得用片段冒充全量。
4. execute_adhoc_query是兜底，不是每次查询的默认入口。只有现有审核工具/结构化数据集
确实无法回答时才使用，使用已验证字段。不要先拉大表或全schema再因裁剪反复查询。
5. 无错误且complete=true、覆盖本次问题时即可回答，不为相同事实反复读回；出现缺值、
分页、身份不符或冲突仍须核实。查询失败/空日期不等于没有记录或未开工。
6. 写入继续使用现有专用事务及原授权/计划/结果核实流程，不使用查询编排执行写入。
7. BOM创建/明细维护/升版用prepare_bom_change，产品关联用prepare_product_bom_binding，
核对计划后execute_bom_change，再reconcile_bom_change。升版须明确当前ROR源版本，
新版本显式复制源明细并成为ROR，旧版保留；unknown不重放，审计用review_bom_change。
8. 设备资料用prepare_resource_change；维护要求启停用prepare_resource_activation（不是Up/Down）；
设置/可用性/状态模型转换用prepare_resource_setup。设备吞吐/普通采集用
prepare_resource_throughput / prepare_resource_collection，核对后execute_resource_change，
再reconcile_resource_change。分组资源采集在本环境不保存样本，执行已关闭，不能把预览成功当落账。
设备吞吐与容器报工分别统计；状态、吞吐及采集历史使用resource分组专用读取工具。
9. 数量损失先区分真实对象类型。父子层级/跟踪载具先get_quantity_loss_scope，再
prepare_related_container_quantity_loss；独立容器用prepare_container_quantity_loss。
明确部分数量或全量及关闭、解关联、生产数量选项；复杂入口generate_rollups=true。
完整相关范围最多8个，实物成员同产品/主单位/步骤；父容器仅明确整棵子树全量。
原生Batch7939禁止ChangeQty，不转换类型或循环成员代扣。归零可能清除共享状态
的载具绑定，须展示完整投影。单次execute_quality_transaction后
reconcile_quality_execution，再review_quality_transaction(action="resolve_history")核实结案；
unknown不重发，不能仅凭HTTP200或同值回读认领成功。
10. SamplingLot整批报废先get_sampling_lot_scrap_readiness（提供明确loss_reason_name），
再prepare_sampling_lot_scrap。ready_for_execution=false表示只读检查不批准事务，
不表示报废未接入。限完整1..20个无冻结、非层级/载具/返工/双单位的基础成员，
须有MES拒收超阈值证据，原因固定为实际LossReason持久ID。正式只执行一次
ExecuteSamplingLot结果3，不循环成员扣量。核对原生父子历史、所有原成员余额/
关闭、原批归属和批级任务保留、成员采样任务清理，再resolve_history结案。
冻结成员拒绝已在隔离批核实未见部分提交；HTTP错误/超时仍保持unknown，
不能凭错误码或无历史自动解除占用，也不推断所有故障均能原子回滚。
11. 单容器计数检验/扣量用prepare_container_inspection；按样本数、拒收数或单组逐原因
由原生测试配置决定，必须明确allow_automatic_quantity_loss，归零另需allow_zero_quantity_loss。
只发一次CollectSamplingData，不补发ChangeQty。新版本非全检计数扣量已有原生父子历史、
全部原因增量及原任务累计核对，reconcile_quality_execution后resolve_history重读结案。
带原因组的历史Rejects为运行累计，不能相加当本次不良；各原因DataValue才是增量。
12. 已核实失败检验的关联事件用prepare_inspection_quality_event，绑定原计划ID/摘要，
明确组织/分类/子分类/优先级/报告人/发现人及带时区报告时间，发生时间取原检验GMT。
独立后续CreateProductionEvent，不重做检验，不修改账号主组织，不挂起/打印/通知。
当前正向验收被MES服务权限拒绝13369354阻止，须管理员开通后新计划继续，未创建成功。
拒绝、错误、超时不得直接当未提交；resolve_rejection仅对覆盖的明确原生拒绝重读完整
原响应、原检验/配置及事件/历史证据，不是通用解锁或权限绕过，不重放原计划。
这些是工具使用建议，不是客户端必然遵循的服务端强制路由；准确性优先于省略必要检查。
"""
