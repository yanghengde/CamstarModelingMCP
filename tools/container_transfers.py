"""T07 bounded intent previews. No Shopfloor writes or invented Split detail payloads."""
import json
import math
import re
import asyncio
from decimal import Decimal

from tools import mcp
from tools.boms import encode
from tools.container_inquiry import get_container_details
from tools.container_trace import get_container_trace
from tools.container_level_rules import check_level_rules
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL, CAMSTAR_QUERY_BASE_URL


async def ancestor_chain(container_id, max_nodes=20):
    """Follow only current ParentContainerId, never historical split/combine edges."""
    out = {'complete': False, 'nodes': [], 'cycle': False,
           'note': '当前父关系逐次读取，不是一致性快照；执行前仍须重新核对。'}
    async def walk():
        current, seen = container_id, set()
        for _ in range(max_nodes):
            if not isinstance(current, str) or not re.fullmatch('[0-9a-fA-F]{16}', current):
                raise ValueError('祖先容器ID格式无效。')
            if current in seen:
                out['cycle'] = True
                raise ValueError('当前父链存在循环。')
            seen.add(current)
            raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                body={'queryText': "SELECT TOP 2 ContainerId,ContainerName,ParentContainerId FROM Container "
                      f"WHERE ContainerId='{current}'"}, response_formatter=raw_json, retry_read_only=True)
            rows = json.loads(raw)
            if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                    or rows[0].get('ContainerId') != current or 'ParentContainerId' not in rows[0]):
                raise ValueError('祖先查询缺失、不唯一或身份不符。')
            row = rows[0]
            out['nodes'].append(row)
            current = row['ParentContainerId']
            if current is None or current == '':
                out['complete'] = True
                return
        raise ValueError('祖先链达到20节点上限，未确认根节点。')
    try:
        await asyncio.wait_for(walk(), timeout=30)
    except (ValueError, TypeError, asyncio.TimeoutError) as error:
        out['error'] = str(error) or '祖先查询超时。'
    return out


def ref(snap):
    return {'name': snap['container']['Name'], 'instanceId': snap['id']}


def same_ref(actual, expected):
    return (isinstance(actual, dict) and actual.get('Name') == expected['name']
            and (actual.get('InstanceId') or actual.get('InstanceID')) == expected['instanceId'])


def echoed_bool(value, expected):
    return type(value) is bool and value == expected or value == str(expected).lower()


async def service_preview(out, body):
    """Fixed non-executing endpoint; never event dispatch or execution fallback."""
    operation = out['operation']
    if operation not in {'Combine', 'Associate', 'Disassociate'}:
        raise ValueError('此服务尚未支持不执行预览。')
    params = {'execute': 'false'}
    if operation == 'Combine':
        params.update({'$select': 'Container,CloseWhenEmpty', '$expand': 'FromContainerDetails'})
    else:
        params['$select'] = 'Container,ChildContainers' + (',DisassociateAll' if operation == 'Disassociate' else '')
    out['request_preview'] = {'path': '/api/' + operation, 'params': params, 'body': body}
    out['server_echo_verified'] = False
    try:
        response = json.loads(await request('POST', '/api/' + operation, body=body, params=params,
                                           base_url=CAMSTAR_SHOPFLOOR_BASE_URL, response_formatter=raw_json))
        data = response.get('data', response)
        if response.get('error') or data.get('error') or data.get('ErrorCode') or _has_next_link(response):
            raise ValueError('服务预览报错或返回不完整。')
        if not same_ref(data.get('Container'), body['container']):
            raise ValueError('服务回显目标容器不一致。')
        if operation == 'Combine':
            rows = data.get('FromContainerDetails')
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError('服务未回显唯一来源明细。')
            row, expected = rows[0], body['fromContainerDetails'][0]
            if (not same_ref(row.get('FromContainer'), expected['fromContainer'])
                    or not echoed_bool(row.get('CombineAllQty'), False)
                    or not echoed_bool(row.get('CloseWhenEmpty'), expected['closeWhenEmpty'])
                    or not echoed_bool(data.get('CloseWhenEmpty'), body['closeWhenEmpty'])):
                raise ValueError('来源或合批标志未正确回显。')
            value = row.get('Qty')
            if isinstance(value, bool) or not Decimal(str(value)).is_finite() or Decimal(str(value)) != Decimal(str(expected['qty'])):
                raise ValueError('合批数量未正确回显。')
            if 'qty2' in expected:
                value = row.get('Qty2')
                if isinstance(value, bool) or not Decimal(str(value)).is_finite() or Decimal(str(value)) != Decimal(str(expected['qty2'])):
                    raise ValueError('合批第二数量未正确回显。')
        else:
            rows = data.get('ChildContainers')
            if not isinstance(rows, list) or len(rows) != len(body['childContainers']):
                raise ValueError('子容器回显不完整。')
            seen = set()
            for row in rows:
                matches = [r for r in body['childContainers'] if same_ref(row, r)]
                if len(matches) != 1 or matches[0]['instanceId'] in seen:
                    raise ValueError('子容器回显不匹配或重复。')
                seen.add(matches[0]['instanceId'])
            if operation == 'Disassociate' and not echoed_bool(data.get('DisassociateAll'), False):
                raise ValueError('disassociateAll=false 未正确回显。')
        out['server_echo_verified'] = True
        out['stage'] = 'server_preview'
        out['server_preview_note'] = 'execute=false 仅验证序列化/回显，不代表业务规则已验证或事务已提交。'
    except (ValueError, TypeError, AttributeError, ArithmeticError) as error:
        out['server_preview_error'] = str(error)


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


def quantity(snap, field='Qty'):
    value = snap['status'].get(field)
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
                                     close_when_empty: bool = False, verify_service: bool = False,
                                     qty2: float | None = None) -> str:
    """单来源数量合批预览。qty2可明确指定有限非负第二数量，不由qty换算；省略保持未知。verify_service=true核对execute=false回显，不执行，不自动合全部数量或搬移子容器。"""
    out = base('Combine')
    try:
        requested = amount(qty)
        secondary = None
        if qty2 is not None:
            if isinstance(qty2, bool) or not isinstance(qty2, (int, float)) or not math.isfinite(qty2) or qty2 < 0:
                raise ValueError('第二数量必须为有限非负数，或省略。')
            secondary = Decimal(str(qty2))
        if type(verify_service) is not bool or type(close_when_empty) is not bool or target_container_name.strip() == source_container_name.strip():
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
        a2, b2 = quantity(source, 'Qty2'), quantity(target, 'Qty2')
        out['intent']['quantity2'] = str(secondary) if secondary is not None else None
        out['quantity2_effect'] = {'source_before': str(a2) if a2 is not None else None,
            'target_before': str(b2) if b2 is not None else None,
            'source_after_if_accepted': str(a2-secondary) if secondary is not None and a2 is not None and a2 >= secondary else None,
            'target_after_if_accepted': str(b2+secondary) if secondary is not None and b2 is not None else None,
            'unit_conversion_performed': False}
        if secondary is None:
            out['blockers'].append('未明确第二数量，不推断为0或按主数量比例换算。')
        elif a2 is None or b2 is None or a2 < secondary:
            out['blockers'].append('第二数量余额未知或不足。')
        out['flag_effects'] = {'close_when_empty': '来源变空后请求关闭' if close_when_empty else '不请求自动关闭'}
        if verify_service:
            detail = {'fromContainer': ref(source), 'qty': qty,
                      'combineAllQty': False, 'closeWhenEmpty': close_when_empty}
            if qty2 is not None:
                detail['qty2'] = qty2
            await service_preview(out, {'container': ref(target), 'closeWhenEmpty': close_when_empty,
                'fromContainerDetails': [detail]})
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


async def family_preview(parent_name, child_names, operation, verify_service=False):
    out = base(operation)
    try:
        if type(verify_service) is not bool:
            raise ValueError('verify_service 必须为布尔值。')
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
        out['blockers'].append('层级规则和容量限制尚未完整核对；不推算父容器数量变化。')
        out['scope'] = 'explicit_direct_children_only'
        out['level_rules'] = await check_level_rules(parent, [r['child'] for r in out['relationships']])
        if not out['level_rules']['complete']:
            out['blockers'].append('静态父/子层级允许列表未完整确认。')
        elif operation == 'Associate' and any(
                c['parent_allows_child'] != 'listed' or c['child_allows_parent'] != 'listed'
                for c in out['level_rules']['checks']):
            out['blockers'].append('所选父子层级未同时出现在双方允许列表中。')
        chain = await ancestor_chain(parent['id'])
        out['ancestor_check'] = chain
        ancestor_ids = {r['ContainerId'] for r in chain['nodes']}
        conflicts = [r['child']['id'] for r in out['relationships'] if r['child']['id'] in ancestor_ids]
        out['relationship_checks_complete'] = chain['complete'] and all(
            r['child']['parent_known'] for r in out['relationships'])
        out['relationship_conflicts'] = conflicts
        if conflicts:
            out['blockers'].append('所选子容器位于目标父容器的当前祖先链中，不能形成此关联。')
        if not chain['complete']:
            out['blockers'].append('当前祖先链未完整核实，不能认定不存在关系环。')
        if chain['nodes'] and parent['parent_known'] and chain['nodes'][0]['ParentContainerId'] != parent['parent_id']:
            out['relationship_checks_complete'] = False
            out['blockers'].append('两次读取的父关系发生变化，须重新预览。')
        if verify_service:
            body = {'container': ref(parent), 'childContainers': [ref(r['child']) for r in out['relationships']]}
            if operation == 'Disassociate':
                body['disassociateAll'] = False
            await service_preview(out, body)
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def preview_container_associate(parent_container_name: str, child_container_names: list[str], verify_service: bool = False) -> str:
    """装箱/父子关联预览，明确1..10个子容器；verify_service=true增加execute=false服务回显核对，不执行。深层环/层级/容量规则待验，回显通过不是业务许可。"""
    return await family_preview(parent_container_name, child_container_names, 'Associate', verify_service)


@mcp.tool
async def preview_container_disassociate(parent_container_name: str, child_container_names: list[str], verify_service: bool = False) -> str:
    """拆箱/解除父子关系预览，仅明确1..10个直属子容器，disassociateAll固定false；verify_service=true增加execute=false服务回显核对，不执行或隐式全部拆箱。"""
    return await family_preview(parent_container_name, child_container_names, 'Disassociate', verify_service)
