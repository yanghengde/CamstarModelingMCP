"""Select model-visible tools without reducing the server's MCP inventory."""
import json
import re

from core.tool_catalog import TOOL_POLICIES

GROUP_TERMS = {
    "collection": r"采集|采样|测量|collection|datapoint",
    "material": r"bom|物料|用料|耗料|投料",
    "resource": r"resource|设备|资源|机台",
    "workflow": r"workflow|工艺|路线|step|步骤|路径|分支|返工",
    "spec": r"spec|规格|工艺|工序",
    "operation": r"operation|工序|工作中心",
    "product": r"product|产品|bom|物料清单",
    "mfgorder": r"mfgorder|工单|订单|生产进度|开工准备|开工配置",
    "container": r"container|容器|追溯|父子|拆分|合并|拆批|合批|装箱|拆箱|split|combine|associate|disassociate|序列号|批次|在制|过站|移站|进站|出站|开工|挂起|释放|返工|不良",
    "reference": r"编号|层级|产线|产品类型|numbering|level|mfgline|producttype",
    "query": r"query|inquiry|sql|历史|状态|进度|在制|追溯",
    "excel": r"excel|csv|表格|附件|导入|上传",
}
WRITE_TERMS = re.compile(r"创建|新建|新增|修改|更新|删除|重建|导入|启动|开工|移站|过站|进站|出站|返工|不良|确认|继续执行|挂起|释放|create|update|delete|patch|start|move|rework|import", re.I)
ROUTING_INSTRUCTION = (
    "用户询问能力、支持的功能或工具参数时，先调用 get_capabilities，据实际目录回答；按 group 查询领域，按 tool_name 查询参数。不得把 registered 说成现场已验收，不得推荐 disabled 工具。"
    "建模完整性检查用 check_modeling_integrity，版本比较用 compare_modeling_versions，线性路线模板复制先 preview_workflow_copy，再确认同一计划执行；不支持的复杂配置不能降级丢弃。"
    "以最新用户问题为当前任务，历史消息仅用于解析省略的对象和上下文；明确切换领域后，不继续执行旧问题或加载旧领域。"
    "连续追问历史时优先查询历史，已有明确容器名称即可使用历史工具，不必重复查询当前状态。"
    "导入中断或写入结果未知时，用 reconcile_workflow_import_plan 只读核对现场；不要自动重试旧计划或宣称已经回滚。"
    "多层容器族谱、父子拆分合并链用 get_container_genealogy，检查 boundaries/failures，不能把遍历受限结果说成完整历史。"
    "容器父子、拆分合并、返工关联用 get_container_trace；检查各 section 的 error/has_more，只对当前读取范围下结论。"
    "Excel 批量导入先 preview_workflow_import，向用户展示逐行结果及重建风险，确认后执行同一 plan_id/plan_digest。工单现场进度优先 get_mfgorder_progress。"
    "工具按任务领域加载。缺少所需工具时先调用 load_tool_group，选择 workflow/spec/operation/"
    "product/mfgorder/container/collection/material/resource/reference/query/excel；下一轮会获得该组定义。查询优先只读；"
    "需要修改时 include_writes=true。工具加载不代表获得业务写入授权。"
    "产品完整工艺链优先 get_product_process；复杂 Workflow 分支、返工和子流程优先 get_workflow_structure。"
    "对象关联详情优先 get_spec_details/get_product_details/get_operation_details/get_mfgorder_details；"
    "容器状态/历史优先 get_container_details/get_container_history；工单前后置过程任务用 get_mfgorder_execution。"
    "检查 complete/pages/warnings，不把空字段当作未配置，不把前后置过程进度当成生产完成数量。"
)


def select_tools(all_tools: list[dict], messages: list[dict]) -> list[dict]:
    users = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
    current = users[-1].lower() if users else ""
    context = current
    # Explicit topic switches should not retain unrelated domains or write intent.
    # For elliptical follow-ups, inherit only the nearest identifiable topic.
    current_groups = {group for group, pattern in GROUP_TERMS.items() if re.search(pattern, current)}
    inherit_topic = not current_groups or (
        current_groups == {"query"} and not re.search(r"query|inquiry|sql", current)
    )
    if inherit_topic:
        for previous in reversed(users[:-1]):
            previous_groups = {group for group, pattern in GROUP_TERMS.items()
                               if re.search(pattern, previous, re.I)}
            # Generic follow-ups such as “再查历史” must not hide the last
            # concrete domain, even after several consecutive follow-ups.
            generic_query = previous_groups == {"query"} and not re.search(
                r"query|inquiry|sql", previous, re.I
            )
            if previous_groups and not generic_query:
                context = previous.lower() + "\n" + current
                break
    wants_write = bool(WRITE_TERMS.search(current))
    if re.search(r"查询|查看|追溯|了解|有哪些|历史", current) and not re.search(
        r"执行|创建|新建|新增|修改|更新|删除|重建|导入|启动|开工|请将|请把|帮我把|create|update|delete|patch|import", current, re.I
    ):
        wants_write = False
    groups = {group for group, pattern in GROUP_TERMS.items() if re.search(pattern, context)}
    if "container" in groups and not wants_write and not re.search(r"workflow|工艺|路线|step|步骤|分支|路径", context):
        groups.discard("workflow")
    if "product" in groups and "workflow" in groups:
        groups.update({"spec", "operation"})
    if re.search(r"完整性|版本对比|版本比较|复制|检查产品", context):
        groups.add("workflow")
    if "container" in groups and wants_write:
        groups.add("reference")
    if "container" in groups and not re.search(r"query|inquiry|sql", context):
        groups.discard("query")
    if "excel" in groups:
        groups.add("workflow")
    write_groups = set(groups) if wants_write else set()
    # Only successful requests in this user turn activate extra groups.
    start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    for message in messages[start:]:
        if message.get("role") == "tool" and message.get("name") == "load_tool_group":
            try:
                data = json.loads(message.get("content", ""))
                group = data.get("group")
                if data.get("loaded") and group in GROUP_TERMS:
                    groups.add(group)
                    if data.get("include_writes") is True:
                        write_groups.add(group)
            except (ValueError, TypeError, AttributeError):
                pass
    # Small discovery set when intent is ambiguous; the model can expand it.
    base = {"get_capabilities", "load_tool_group", "get_mcp_server_status"}
    if not groups:
        base.update({"list_workflows", "list_specs", "list_products", "list_mfgorders"})
    selected = []
    for tool in all_tools:
        name = tool["function"]["name"]
        policy = TOOL_POLICIES.get(name)
        if not policy or not policy.default_visible:
            continue
        if name in base or (policy.group in groups and (policy.effect == "read" or policy.group in write_groups)):
            selected.append(tool)
    return selected


def selection_metrics(selected: list[dict], total: int) -> dict:
    return {"available_tool_count": total, "selected_tool_count": len(selected),
            "tool_schema_chars": len(json.dumps(selected, ensure_ascii=False)),
            "selected_tools": [t["function"]["name"] for t in selected]}
