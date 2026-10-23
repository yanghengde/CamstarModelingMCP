"""Request additional model tool definitions; never executes business tools."""
import json
from typing import Literal

from core.tool_catalog import TOOL_POLICIES
from tools import mcp


@mcp.tool
async def load_tool_group(
    group: Literal["material", "resource", "workflow", "spec", "operation", "product", "mfgorder", "container", "reference", "query", "excel"],
    include_writes: bool = False,
) -> str:
    """Load a tool group on the NEXT model round when needed tools are missing.

    workflow=routes/steps; spec=specifications; operation=operations;
    product=products; mfgorder=orders; container=execution/status/history;
    material=BOM/material requirements/version comparison; resource=equipment/groups/spec resource context; reference=levels/numbering/lines/types; query=general Inquiry; excel=import.
    include_writes exposes write definitions only, never authorizes execution.
    """
    names = [name for name, policy in TOOL_POLICIES.items()
             if policy.group == group and policy.default_visible
             and (policy.effect == "read" or include_writes)]
    return json.dumps({"loaded": bool(names), "group": group,
                       "include_writes": include_writes, "tools": names}, ensure_ascii=False)

@mcp.tool
async def get_capabilities(group: str | None = None, tool_name: str | None = None) -> str:
    """查询本系统真实能力目录。回答你能做什么、支持哪些操作、参数和限制时先调用。
    Omit arguments for a grouped overview; use group for tools in that domain,
    or exact tool_name for its actual description and complete input schema.
    Read-only local registry inspection, never executes a business tool.
    """
    registered = {tool.name: tool for tool in await mcp.list_tools()}
    labels = {'material': 'BOM 与用料', 'resource': '设备资源与工序资源配置', 'workflow': '工艺路线', 'spec': '规格与工序配置', 'operation': '操作定义',
              'product': '产品', 'mfgorder': '工单及现场进度', 'container': '容器与生产事务',
              'reference': '基础资料与原因码', 'query': '查询服务', 'excel': '表格导入', 'system': '系统与能力发现'}
    disabled = {'patch_mfgorder': '当前服务 PATCH 契约未验证，工具已禁用；使用 update_mfgorder。'}
    caveats = {
        'hold_container': '已封装、测试通过；未完成现场写入验收。',
        'release_container': '仅容器解冻，不是工单释放；未完成现场写入验收。',
        'change_container_quantity': '产品数量变更，不等于报废或报工；未完成现场写入验收。',
        'get_mfgorder_progress': '分布统计仅覆盖当前页，不等同工单完成数量。',
        'get_mfgorder_execution': '前后置过程任务信息，不等同生产产量。',
    }
    result = {'source': 'live MCP registry + explicit tool policies',
              'limitations': ['工具已注册不代表现场事务验收通过；权限、配置和状态由产品校验。',
                              '未提供已验证的独立工单下发、整单报工、生产容器报废能力。',
                              '目录查询不加载业务工具，不授予写入权限。']}
    if group is not None and group not in labels:
        return json.dumps({'error': '未知分组', 'groups': labels}, ensure_ascii=False)
    if tool_name:
        policy = TOOL_POLICIES.get(tool_name)
        tool = registered.get(tool_name)
        if not policy or not tool:
            return json.dumps({'error': '未找到已注册且列入目录的工具'}, ensure_ascii=False)
        result['tool'] = {'name': tool_name, 'group': policy.group, 'effect': policy.effect,
                          'status': 'disabled' if tool_name in disabled else 'registered',
                          'description': tool.description, 'input_schema': tool.parameters,
                          'always_confirm_in_app': policy.always_confirm,
                          'limitations': disabled.get(tool_name) or caveats.get(tool_name)}
    else:
        entries = []
        for name, policy in TOOL_POLICIES.items():
            if name not in registered or not policy.default_visible or (group and policy.group != group):
                continue
            entries.append({'name': name, 'group': policy.group, 'effect': policy.effect,
                            'status': 'disabled' if name in disabled else 'registered',
                            'description': registered[name].description if group else None,
                            'limitations': disabled.get(name) or caveats.get(name)})
        result['groups'] = [{'group': key, 'label': label,
                             'tools': [e if group else {k: v for k, v in e.items() if k != 'description'}
                                       for e in entries if e['group'] == key]}
                            for key, label in labels.items() if not group or key == group]
        result['next'] = '使用 tool_name 查询完整参数；执行前通过 load_tool_group 加载对应定义。'
    return json.dumps(result, ensure_ascii=False)
