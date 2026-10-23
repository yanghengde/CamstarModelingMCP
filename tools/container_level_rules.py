"""Static parent/child level allowlists. Empty/missing configuration is not permission."""
from tools.process_context import ReadContext
from tools.modeling_details import _has_next_link


def identity(ref):
    if not isinstance(ref, dict):
        return None
    return ref.get('InstanceID') or ref.get('InstanceId')


def membership(rows, target):
    if not isinstance(rows, list) or not rows:
        return 'unknown'
    malformed = False
    for row in rows:
        if not isinstance(row, dict) or not row.get('Name'):
            malformed = True
            continue
        if identity(row) == target['InstanceID']:
            return 'listed' if row['Name'] == target['Name'] else 'unknown'
        if row['Name'] == target['Name']:
            return 'listed' if not identity(row) else 'unknown'
    return 'unknown' if malformed else 'not_listed'


async def check_level_rules(parent, children):
    out = {'complete': False, 'capacity_verified': False, 'checks': [],
           'note': '仅静态层级允许列表；空/缺失不是无限制。不将配置差异用于阻止解除已有父子关系。容量契约未确认。'}
    cache = {}
    ctx = ReadContext(max_requests=24)
    async def read(snap):
        ref = snap.get('status', {}).get('ContainerLevel')
        if not isinstance(ref, dict) or not ref.get('Name'):
            raise ValueError('容器层级引用缺失。')
        key = (identity(ref), ref['Name'])
        if key not in cache:
            obj = await ctx.resolve('ContainerLevels', ref, revisioned=False)
            expanded = await ctx.get('ContainerLevels', obj['InstanceID'], {'$expand': 'ChildLevels,ParentLevels'})
            if expanded.get('InstanceID') != obj['InstanceID'] or expanded.get('Name') != obj['Name'] or _has_next_link(expanded):
                raise ValueError('层级展开身份不匹配或分页不完整。')
            cache[key] = expanded
        return cache[key]
    try:
        parent_level = await read(parent)
        out['parent_level'] = parent_level
        for child in children:
            check = {'child_id': child['id'], 'parent_allows_child': 'unknown', 'child_allows_parent': 'unknown'}
            out['checks'].append(check)
            try:
                child_level = await read(child)
                check.update(child_level=child_level,
                    parent_allows_child=membership(parent_level.get('ChildLevels'), child_level),
                    child_allows_parent=membership(child_level.get('ParentLevels'), parent_level))
            except (ValueError, TypeError, KeyError) as error:
                check['error'] = str(error)
        out['complete'] = bool(out['checks']) and all('unknown' not in (c['parent_allows_child'], c['child_allows_parent']) for c in out['checks'])
    except (ValueError, TypeError, KeyError) as error:
        out['error'] = str(error)
    return out
