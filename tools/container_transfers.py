"""T07 bounded intent previews. No Shopfloor writes or invented Split detail payloads."""
import json
import math
import re
from decimal import Decimal

from tools import mcp
from tools.boms import encode
from tools.container_inquiry import get_container_details
from tools.container_trace import get_container_trace


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError('数量必须为有限正数。')
    return Decimal(str(value))


def names(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 10:
        raise ValueError('每次明确指定1..10个容器。')
    if any(not isinstance(x, str) or not x.strip() for x in values):
        raise ValueError('容器名称不能为空。')
    values = [x.strip() for x in values]
    if len(values) != len(set(values)):
        raise ValueError('不能重复指定同一容器。')
    return values


def base(operation):
    return {'operation': operation, 'complete': False, 'executable': False, 'stage': 'intent_preview',
            'blockers': ['服务执行规则、单位/第二数量、权限及提交后历史关联尚待验证。'],
            'note': '只读意图预览，不是执行许可；数量为条件计算，父子关系不等同物料合批。'}


async def snapshot(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('容器名称必填。')
    result = json.loads(await get_container_details(name.strip(), view='status'))
    data = result.get('data', {}).get('ContainerStatusDetails', {})
    ref = data.get('Container', {})
    cid = ref.get('InstanceId') or ref.get('InstanceID')
    if result.get('error') or ref.get('Name') != name.strip() or not isinstance(cid, str) or not re.fullmatch('[0-9a-fA-F]{16}', cid):
        raise ValueError('容器身份未确认；重名或不完整响应不能用于预览。')
    keys = ('Qty', 'Qty2', 'UOM', 'UOM2', 'Product', 'ContainerLevel', 'Status', 'StatusName', 'Spec', 'Step', 'InProcess', 'InRework')
    out = {'container': ref, 'id': cid, 'status': {k: data.get(k) for k in keys},
           'unknown_fields': [k for k in keys if data.get(k) is None]}
    trace = json.loads(await get_container_trace(name.strip(), cid, section='current_family', limit=50))
    family = trace.get('sections', {}).get('current_family', {})
    rows = family.get('rows')
    own = [r for r in rows if isinstance(r, dict) and r.get('ContainerId') == cid] if isinstance(rows, list) else []
    out['family_complete'] = (not trace.get('error') and family.get('page_complete') is True
                              and family.get('has_more') is False and len(own) == 1)
    out['family'] = rows if isinstance(rows, list) else []
    out['parent_known'] = bool(out['family_complete'] and 'ParentContainerId' in own[0])
    out['parent_id'] = own[0].get('ParentContainerId') if own else None
    return out


def quantity(snap):
    value = snap['status'].get('Qty')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return Decimal(str(value))


@mcp.tool
async def preview_container_split(container_name: str, new_container_name: str, qty: float,
                                   close_when_empty: bool = False, reduce_qty: bool = True) -> str:
    """拆批数量意图预览，不执行。明确新容器名、数量、是否扣减/空后关闭。Split.toContainerDetails为系统历史字段，当前不构造数量提交payload；新名称可用性和现场规则待验。"""
    out = base('Split')
    try:
        requested = amount(qty)
        if type(close_when_empty) is not bool or type(reduce_qty) is not bool:
            raise ValueError('close_when_empty 和 reduce_qty 必须为布尔值。')
        if not new_container_name.strip() or new_container_name.strip() == container_name.strip():
            raise ValueError('必须明确不同于来源的新容器名。')
        source = await snapshot(container_name)
        before = quantity(source)
        out.update(source=source, intent={'new_container_name': new_container_name.strip(), 'quantity': str(requested),
                   'close_when_empty': close_when_empty, 'reduce_qty': reduce_qty},
                   quantity_effect={'source_before': str(before) if before is not None else None,
                   'source_after_if_accepted': str(before-requested if reduce_qty else before) if before is not None else None,
                   'new_quantity_if_accepted': str(requested)}, new_name_available=None)
        if before is None:
            out['blockers'].append('来源数量未知。')
        elif requested > before:
            out['blockers'].append('拆出数量超过来源现有数量。')
            out['quantity_effect']['source_after_if_accepted'] = None
        out['blockers'].append('toContainerDetails 为系统生成历史字段；数量拆批的实际输入路径尚未核实。')
        out['flag_effects'] = {'reduce_qty': '扣减来源数量' if reduce_qty else '不扣减来源数量，不能按数量守恒解释',
                               'close_when_empty': '来源变空后请求关闭' if close_when_empty else '不请求自动关闭'}
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def preview_container_combine(target_container_name: str, source_container_name: str, qty: float,
                                     close_when_empty: bool = False) -> str:
    """单来源到现有目标的数量合批意图预览。不执行，不自动合全部数量或搬移子容器；核对身份、产品版本、层级和当前数量，单位等未知时不宣称可执行。"""
    out = base('Combine')
    try:
        requested = amount(qty)
        if type(close_when_empty) is not bool or target_container_name.strip() == source_container_name.strip():
            raise ValueError('来源与目标须不同，close_when_empty 必须为布尔值。')
        target, source = await snapshot(target_container_name), await snapshot(source_container_name)
        if target['id'] == source['id']:
            raise ValueError('来源与目标解析为同一容器。')
        out.update(source=source, target=target, intent={'quantity': str(requested), 'combine_all_qty': False,
                   'close_when_empty': close_when_empty}, comparisons={})
        for field in ('Product', 'ContainerLevel', 'UOM', 'UOM2', 'Spec'):
            left, right = source['status'][field], target['status'][field]
            state = 'unknown' if left is None or right is None else 'same' if left == right else 'different'
            out['comparisons'][field] = state
            if state != 'same':
                out['blockers'].append(f'{field} 为 {state}，兼容性未确认。')
        a, b = quantity(source), quantity(target)
        out['quantity_effect'] = {'source_before': str(a) if a is not None else None,
            'target_before': str(b) if b is not None else None,
            'source_after_if_accepted': str(a-requested) if a is not None and a >= requested else None,
            'target_after_if_accepted': str(b+requested) if b is not None else None}
        if a is None or b is None or a < requested:
            out['blockers'].append('来源/目标数量未知或来源余额不足。')
        out['flag_effects'] = {'close_when_empty': '来源变空后请求关闭' if close_when_empty else '不请求自动关闭'}
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


async def family_preview(parent_name, child_names, operation):
    out = base(operation)
    try:
        children = names(child_names)
        if parent_name.strip() in children:
            raise ValueError('父容器不能作为自己的子容器。')
        parent = await snapshot(parent_name)
        out.update(parent=parent, relationships=[], disassociate_all=False)
        seen = {parent['id']}
        for name in children:
            child = await snapshot(name)
            if child['id'] in seen:
                raise ValueError('容器引用解析为重复或自关联ID。')
            seen.add(child['id'])
            before = child['parent_id'] if child['parent_known'] else 'unknown'
            if before == 'unknown':
                out['blockers'].append(f'{name} 的当前父关系未知。')
            elif operation == 'Associate' and before:
                out['blockers'].append(f'{name} 已有父容器，不能默认为可直接装箱。')
            elif operation == 'Disassociate' and before != parent['id']:
                out['blockers'].append(f'{name} 不是已确认的目标父容器直属子容器。')
            if parent.get('parent_id') == child['id']:
                out['blockers'].append('所选子容器是当前父容器的父级，可能形成关系环。')
            out['relationships'].append({'child': child, 'parent_before': before,
                'parent_after_if_accepted': parent['id'] if operation == 'Associate' else None})
        out['blockers'].append('更深层祖先环、层级规则和容量限制尚未完整核对；不推算父容器数量变化。')
        out['scope'] = 'explicit_direct_children_only'
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def preview_container_associate(parent_container_name: str, child_container_names: list[str]) -> str:
    """装箱/父子关联意图预览，只读，明确1..10个子容器，核对已有父关系。不等同数量合批；深层环/层级/容量规则待验。"""
    return await family_preview(parent_container_name, child_container_names, 'Associate')


@mcp.tool
async def preview_container_disassociate(parent_container_name: str, child_container_names: list[str]) -> str:
    """拆箱/解除父子关系意图预览，只读，仅明确1..10个直属子容器，disassociateAll固定false；不提供隐式全部拆箱，不等同数量拆批。"""
    return await family_preview(parent_container_name, child_container_names, 'Disassociate')
