"""Static collection configuration; no CollectData transaction or quality verdict."""
import json
from typing import Literal

from tools import mcp
from tools.boms import bom_ref, encode
from tools.process_context import ReadContext
from tools.modeling_details import _has_next_link
from tools.container_inquiry import get_container_details

TYPES = {1: 'Integer', 2: 'Float', 3: 'Fixed', 4: 'String', 5: 'Object', 6: 'TimeStamp', 7: 'Boolean', 9: 'Decimal'}
NOTE = '仅静态定义；未验证当前事务动态必填、权限或质量判定。null/缺失约束为未知，不推断无限制。'


async def definition(ctx, ref, kind, offset, limit):
    entity = {'user': 'UserDataCollectionDefs', 'point': 'DataPointCollectionDefs', 'base': 'DataCollectionDefs'}[kind]
    obj = await ctx.resolve(entity, ref)
    result = {'source': entity, 'definition': obj, 'complete': False, 'constraints_complete': False, 'note': NOTE, 'warnings': []}
    if kind == 'base':
        result['warnings'].append('基础定义未确认具体采集点类型；请明确 user 或 point 类型后读取。')
        return result
    field = 'DataPoints' if kind == 'user' else 'Groups'
    expanded = await ctx.get(entity, obj['InstanceID'], {'$expand': field if kind == 'user' else 'Groups($expand=Points)'})
    if any(expanded.get(k) != obj.get(k) for k in ('InstanceID', 'Name', 'Revision')):
        raise ValueError('展开响应定义与已定位版本不一致。')
    rows = expanded.get(field)
    result['definition'] = {k: v for k, v in expanded.items() if k != field}
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        result['warnings'].append(f'{field} 未返回有效列表。')
        return result
    page = rows[offset:offset + limit]
    result.update(complete=offset == 0 and len(rows) <= limit and not _has_next_link(expanded),
                  page={'offset': offset, 'limit': limit, 'available_count': len(rows),
                        'server_total_known': not _has_next_link(expanded),
                        'next_offset': offset + limit if offset + limit < len(rows) else None},
                  raw_items=page)
    if kind == 'point':
        result['groups'] = []
        for group in page:
            if not group.get('Name'):
                result['complete'] = False
            points = group.get('Points')
            group_out = {'name': group.get('Name'), 'minimum_samples': group.get('MinimumSamples'), 'points': []}
            result['groups'].append(group_out)
            if not isinstance(points, list) or any(not isinstance(p, dict) for p in points):
                result['complete'] = False
                result['warnings'].append('采集组 Points 未完整返回。')
                continue
            if len(points) > limit:
                result['complete'] = False
                result['warnings'].append('组内采集点达到单组输出上限，请缩小查询范围。')
            for p in points[:limit]:
                group_out['points'].append({
                    'name': p.get('Name'), 'data_type': {'raw': p.get('DataType'), 'label': TYPES.get(p.get('DataType'), '未知')},
                    'required': p.get('IsRequired'), 'minimum_samples': p.get('MinimumSamples'), 'uom': p.get('UOM'),
                    'warning_limits': {'lower': p.get('InnerLowerLimit'), 'upper': p.get('InnerUpperLimit')},
                    'spec_limits': {'lower': p.get('OuterLowerLimit'), 'upper': p.get('OuterUpperLimit')},
                    'allowed_values': {'status': 'unknown'}})
                if not p.get('Name') or p.get('DataType') not in TYPES or not isinstance(p.get('IsRequired'), bool):
                    result['complete'] = False
        result['warnings'].append('分组点嵌套展开依据 metadata；非空现场验收待补。限值保留原始表达式，不执行或推算。')
    else:
        result['points'] = []
        for row in page:
            unknown = [k for k in ['DataType', 'IsRequired', 'UOM', 'LowerLimit', 'UpperLimit'] if row.get(k) is None]
            point = {'name': row.get('Name'), 'data_type': {'raw': row.get('DataType'), 'label': TYPES.get(row.get('DataType'), '未知')},
                     'required': row.get('IsRequired'), 'uom': row.get('UOM'),
                     'lower_limit': row.get('LowerLimit'), 'upper_limit': row.get('UpperLimit'),
                     'unknown_constraints': unknown,
                     'allowed_values': {'status': 'unknown', 'note': '未验证统一枚举选值契约；不根据数字类型猜测。'}}
            if not row.get('Name') or row.get('DataType') not in TYPES or not isinstance(row.get('IsRequired'), bool):
                result['complete'] = False
                result['warnings'].append('采集点名称、类型或必填标记无法确认。')
            result['points'].append(point)
    result['constraints_complete'] = False
    return result


@mcp.tool
async def get_data_collection_details(name: str = '', revision: str = '', instance_id: str | None = None,
                                       use_ror: bool = False, kind: Literal['user', 'point', 'base'] = 'user',
                                       offset: int = 0, limit: int = 50) -> str:
    """只读采集定义。明确 ID/名称版本/ROR；user 展开 DataPoints，point 展开 Groups/Points，base 不猜子类型。返回原始类型/约束及未知项，不执行采集、不判断质量。"""
    try:
        if kind not in {'user', 'point', 'base'} or offset < 0 or not 1 <= limit <= 200:
            raise ValueError('kind=user/point/base，offset>=0，limit=1..200。')
        return encode(await definition(ReadContext(max_requests=4), bom_ref(name, revision, instance_id, use_ror), kind, offset, limit))
    except ValueError as error:
        return encode({'complete': False, 'error': str(error), 'note': NOTE})


@mcp.tool
async def get_container_collection_requirements(container_name: str, container_instance_id: str | None = None,
                                                 transaction_name: str = '', max_definitions: int = 10,
                                                 definition_kind: Literal['base', 'user', 'point'] = 'base') -> str:
    """只读容器当前 Spec 的 TxnMap 采集关联。transaction_name 精确匹配 TxnName；不提供则列出所有静态映射。基础定义不猜具体子类型，动态要求始终未验证，无 Shopfloor 写入。"""
    out = {'complete': False, 'source': 'Query/ContainerInfoInquiry -> Specs/TxnMap -> DataCollectionDefs',
           'note': NOTE, 'dynamic_requirements_verified': False, 'mappings': [], 'warnings': []}
    try:
        if not container_name.strip() or not 1 <= max_definitions <= 30 or definition_kind not in {'base', 'user', 'point'}:
            raise ValueError('容器名称必填，max_definitions=1..30。')
        info = json.loads(await get_container_details(container_name, container_instance_id, view='info'))
        payload = info.get('data', {})
        if info.get('error') or not isinstance(payload, dict):
            raise ValueError('容器查询失败。')
        container = payload.get('Container')
        if not isinstance(container, dict) or container.get('Name') != container_name.strip():
            raise ValueError('容器身份未确认。')
        if container_instance_id and (container.get('InstanceId') or container.get('InstanceID')) != container_instance_id.strip():
            raise ValueError('容器 ID 不匹配。')
        ctx = ReadContext(max_requests=4 + 3 * max_definitions)
        if not payload.get('CurrentSpec'):
            out['status'] = 'current_spec_unknown'
            raise ValueError('容器查询未返回当前 Spec，无法确认工序采集要求。')
        spec = await ctx.resolve('Specs', payload.get('CurrentSpec'))
        out['spec'] = {k: spec.get(k) for k in ('InstanceID', 'Name', 'Revision')}
        expanded = await ctx.get('Specs', spec['InstanceID'], {'$expand': 'TxnMap'})
        if expanded.get('InstanceID') != spec['InstanceID']:
            raise ValueError('工序展开响应 ID 不匹配。')
        rows = expanded.get('TxnMap')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('TxnMap 未返回有效列表，不能认定无需采集。')
        selected = [r for r in rows if not transaction_name or r.get('TxnName') == transaction_name]
        out.update(transaction_filter=transaction_name or None, static_mapping_count=len(selected),
                   static_mappings_complete=not _has_next_link(expanded) and len(selected) <= max_definitions and info.get('complete') is True)
        for row in selected[:max_definitions]:
            item = {'transaction': row.get('TxnName'), 'required': row.get('Required'), 'sequence': row.get('Sequence'),
                    'definition_ref': row.get('DataCollectionDef')}
            try:
                item['definition'] = await definition(ctx, row.get('DataCollectionDef'), definition_kind, 0, 50)
            except ValueError as error:
                item['error'] = str(error)
                out['static_mappings_complete'] = False
            out['mappings'].append(item)
        if not selected:
            out['warnings'].append('未匹配静态映射，不代表当前事务不要求采集。')
        out['warnings'].append('默认返回基础定义；可用 definition_kind 明确采集子类型，动态要求始终尚未核实。')
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)
