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
这些是工具使用建议，不是客户端必然遵循的服务端强制路由；准确性优先于省略必要检查。
"""
