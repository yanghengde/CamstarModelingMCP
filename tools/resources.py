"""Read-only resource configuration; configured candidates are not dispatch eligibility."""
from tools import mcp
from tools.process_context import ReadContext
from tools.modeling_details import raw_json, _has_next_link

NOTE = "配置候选不代表允许开工；未返回/null 不等于未配置。非空资源现场样本尚待验收。"


def reference(name, instance_id, revision=None):
    if not (name.strip() or (instance_id or '').strip()):
        raise ValueError('请提供名称或 InstanceID。')
    ref = {}
    if name.strip():
        ref['Name'] = name.strip()
    if instance_id:
        ref['InstanceID'] = instance_id.strip()
    if revision:
        ref['Revision'] = revision
    return ref


def result(source):
    return {'source': source, 'complete': True, 'warnings': [], 'note': NOTE}


def fail(out, message):
    out['complete'] = False
    out['warnings'].append(str(message))


@mcp.tool
async def list_resources(name: str = '', offset: int = 0, limit: int = 20) -> str:
    """分页读取设备资源配置；name 为可选精确名称。检查 has_more/complete，不将空页视为全库为空。"""
    out = result('Modeling/Resources')
    if not 1 <= limit <= 100 or offset < 0:
        fail(out, 'limit 应为 1..100，offset >= 0。')
        return raw_json(out)
    params = {'$top': limit + 1, '$skip': offset, '$orderby': 'Name,InstanceID'}
    if name.strip():
        params['$filter'] = "Name eq '" + name.strip().replace("'", "''") + "'"
    try:
        data = await ReadContext().get('Resources', params=params)
        rows = data.get('value')
        if not isinstance(rows, list):
            raise ValueError('响应缺少 value 列表。')
        more = len(rows) > limit or _has_next_link(data)
        out.update(resources=rows[:limit], offset=offset, limit=limit, has_more=more,
                   next_offset=offset + limit if more else None, complete=not more and offset == 0)
    except ValueError as error:
        fail(out, error)
    return raw_json(out)


@mcp.tool
async def get_resource_details(name: str = '', instance_id: str | None = None,
                               include_maintenance: bool = False) -> str:
    """按名称或 InstanceID 读取资源、层级和 Setup。可展开 PMStatus；返回原始状态，不能据此认定可开工。"""
    out = result('Modeling/Resources')
    ctx = ReadContext(max_requests=4)
    try:
        data = await ctx.resolve('Resources', reference(name, instance_id), revisioned=False)
        out['data'] = data
        out['availability'] = {'raw': data.get('Availability'),
                               'label': {1: 'Up (Available)', 2: 'Down (Not available)'}.get(data.get('Availability'), '未知')}
        missing = [key for key in ['Availability', 'ParentResource', 'ChildResources', 'CurrentSetup'] if data.get(key) is None]
        out['unknown_fields'] = missing
        if missing:
            fail(out, '部分配置字段未返回或为 null。')
        if _has_next_link(data):
            fail(out, '资源响应包含后续页。')
        if include_maintenance:
            detail = await ctx.get('Resources', data['InstanceID'], {'$expand': 'PMStatus'})
            out['maintenance'] = detail.get('PMStatus')
            if not isinstance(out['maintenance'], list) or _has_next_link(detail):
                fail(out, '维护状态未完整返回。')
    except ValueError as error:
        fail(out, error)
    return raw_json(out)


@mcp.tool
async def get_spec_resource_context(name: str = '', revision: str = '',
                                    instance_id: str | None = None,
                                    max_groups: int = 30, max_resources: int = 100,
                                    max_depth: int = 8) -> str:
    """读取 Spec 配置的资源组及嵌套组候选；无 ID 时必须明确名称/版本，不自动猜 ROR。去重并报告循环、缺失引用和边界；不是实时设备派工校验。"""
    out = result('Modeling/Specs -> ResourceGroups -> Resources')
    out.update(groups=[], resources=[], cycles=[])
    if not 1 <= max_groups <= 100 or not 1 <= max_resources <= 200 or not 1 <= max_depth <= 20:
        fail(out, 'max_groups=1..100，max_resources=1..200，max_depth=1..20。')
        return raw_json(out)
    ctx = ReadContext(max_requests=2 + 2 * max_groups + 2 * max_resources)
    groups, resources = set(), set()

    async def visit(ref, ancestors, depth):
        if depth > max_depth or len(groups) >= max_groups:
            fail(out, '资源组遍历达到深度或数量上限。')
            return
        try:
            group = await ctx.resolve('ResourceGroups', ref, revisioned=False)
        except ValueError as error:
            fail(out, error)
            return
        key = group['InstanceID']
        if key in ancestors:
            out['cycles'].append(ancestors + [key])
            fail(out, '资源组存在循环引用。')
            return
        if key in groups:
            return
        groups.add(key)
        out['groups'].append(group)
        if _has_next_link(group):
            fail(out, '资源组响应包含后续页。')
        entries = group.get('Entries')
        children = group.get('Groups')
        if not isinstance(entries, list) or not isinstance(children, list):
            fail(out, '资源组 Entries/Groups 未完整返回，不推断为空。')
        for member in entries if isinstance(entries, list) else []:
            if len(resources) >= max_resources:
                fail(out, '候选资源达到数量上限。')
                break
            try:
                resource = await ctx.resolve('Resources', member, revisioned=False)
                resource_id = resource['InstanceID']
                if resource_id not in resources:
                    resources.add(resource_id)
                    out['resources'].append(resource)
                if _has_next_link(resource):
                    fail(out, '候选资源响应包含后续页。')
            except ValueError as error:
                fail(out, error)
        for child in children if isinstance(children, list) else []:
            await visit(child, ancestors + [key], depth + 1)

    try:
        spec = await ctx.resolve('Specs', reference(name, instance_id, revision))
        out['spec'] = {'InstanceID': spec['InstanceID'], 'Name': spec.get('Name'), 'Revision': spec.get('Revision')}
        if _has_next_link(spec):
            fail(out, 'Spec 响应包含后续页。')
        group_ref = spec.get('ResourceGroup')
        if not group_ref:
            fail(out, 'Spec ResourceGroup 未返回或为空，无法确认资源配置。')
        else:
            await visit(group_ref, [], 1)
    except ValueError as error:
        fail(out, error)
    return raw_json(out)
