"""Bounded read-only configuration evidence, never authorizes production start."""
import asyncio
import json

from tools import mcp
from tools.process_context import ReadContext, get_product_process
from tools.modeling_workspace import inspect_structure
from tools.resources import get_spec_resource_context, reference
from tools.boms import get_product_material_requirements, encode
from tools.modeling_details import _has_next_link


async def decoded(task):
    try:
        value = json.loads(await task)
        return value if isinstance(value, dict) else {'complete': False, 'error': '响应不是对象。'}
    except (ValueError, TypeError) as error:
        return {'complete': False, 'error': str(error)}


@mcp.tool
async def check_mfgorder_readiness(mfgorder_name: str, max_specs: int = 5, max_depth: int = 2) -> str:
    """只读工单开工配置检查：产品工艺、资源、标准BOM/工单物料、Spec静态采集映射。返回通过/缺失/未知/不适用及证据；不检查库存、资质或动态事务规则，不执行Start。max_specs=1..10，max_depth=0..3。"""
    out = {'complete': False, 'ready_to_start': None, 'checks': [],
           'scope': '工单关联及已读取范围的静态配置；不是开工许可或全部业务规则验收。',
           'limits': {'max_specs': max_specs, 'max_depth': max_depth, 'concurrency': 2,
                      'logical_request_upper_bound': 178 + 33 * max_specs,
                      'note': '复用各领域请求预算之和；HTTP 层 GET 重试最多使实际尝试数翻倍。'},
           'unexamined_specs': []}

    def add(area, obj, status, source, detail, action):
        out['checks'].append({'area': area, 'object': obj, 'status': status,
                              'source': source, 'detail': detail, 'next_action': action})

    if not mfgorder_name.strip() or not 1 <= max_specs <= 10 or not 0 <= max_depth <= 3:
        out['error'] = '工单名称必填；max_specs=1..10，max_depth=0..3。'
        return encode(out)
    ctx = ReadContext(max_requests=4)
    try:
        order = await ctx.resolve('MfgOrders', reference(mfgorder_name, None), revisioned=False)
        product = await ctx.resolve('Products', order.get('Product'))
        out['order'] = {k: order.get(k) for k in ('InstanceID', 'Name', 'Qty')}
        out['product'] = {k: product.get(k) for k in ('InstanceID', 'Name', 'Revision')}
        add('工单产品关联', mfgorder_name, '通过', 'Modeling/MfgOrders -> Products', '已唯一解析工单及产品版本。', '无需处理此关联。')
    except ValueError as error:
        add('工单产品关联', mfgorder_name, '未知', 'Modeling/MfgOrders -> Products', str(error), '核对工单名称、产品引用与权限。')
        out['summary'] = '无法确认工单产品关联，未继续推导开工配置。'
        return encode(out)

    process, material = await asyncio.gather(
        decoded(get_product_process(product['InstanceID'], max_depth=max_depth)),
        decoded(get_product_material_requirements(instance_id=product['InstanceID'], mfgorder_name=mfgorder_name, limit=50)))
    inspection = inspect_structure(process)
    process_ok = process.get('complete') is True and bool(process.get('workflows')) and not inspection['issues']
    add('工艺配置', product['InstanceID'], '通过' if process_ok else '未知', 'get_product_process + inspect_structure',
        {'complete': process.get('complete'), 'issues': inspection['issues'], 'error': process.get('error')}, '有待核对项时查看工艺完整性明细。')
    for key, label in [('standard_bom', '标准BOM'), ('order_materials', '工单物料')]:
        view = material.get(key, {})
        rows = view.get('data', {}).get('MaterialList')
        ok = view.get('complete') is True and isinstance(rows, list) and bool(rows)
        add(label, mfgorder_name, '通过' if ok else '未知', 'get_product_material_requirements/' + key,
            {'read_complete': view.get('complete'), 'returned_rows': len(rows) if isinstance(rows, list) else None,
             'error': view.get('error') or material.get('error'), 'note': '仅确认清单可读取；空清单是否允许需现场规则。'},
            '核对清单适用性、数量单位；库存和损耗不在本次结论内。')

    specs = process.get('specs') or {}
    ids = sorted(specs)
    out['unexamined_specs'] = ids[max_specs:]
    sem = asyncio.Semaphore(2)

    async def inspect_spec(identifier):
        async with sem:
            resource = await decoded(get_spec_resource_context(instance_id=identifier, max_groups=5, max_resources=10, max_depth=3))
            try:
                spec = await ReadContext(max_requests=1).get('Specs', identifier, {'$expand': 'TxnMap'})
                if spec.get('InstanceID') != identifier:
                    raise ValueError('Spec ID 不一致。')
                mappings = spec.get('TxnMap')
                collection = {'read_complete': isinstance(mappings, list) and not _has_next_link(spec),
                              'mapping_count': len(mappings) if isinstance(mappings, list) else None,
                              'dynamic_requirements_verified': False}
            except ValueError as error:
                collection = {'read_complete': False, 'error': str(error)}
            return identifier, resource, collection

    for identifier, resource, collection in await asyncio.gather(*(inspect_spec(i) for i in ids[:max_specs])):
        ok = resource.get('complete') is True and bool(resource.get('resources'))
        add('工序资源', identifier, '通过' if ok else '未知', 'get_spec_resource_context',
            {'read_complete': resource.get('complete'), 'candidate_count': len(resource.get('resources', [])),
             'warnings': resource.get('warnings'), 'error': resource.get('error')}, '候选配置不代表设备实时可用；核对缺失引用及现场限制。')
        add('工序采集', identifier, '未知', 'Specs/TxnMap', collection, '按具体事务核对采集定义、点约束及动态要求。')
    if not ids or out['unexamined_specs']:
        add('工序检查范围', product['InstanceID'], '未知', 'get_product_process/specs',
            {'resolved_specs': len(ids), 'unexamined_specs': out['unexamined_specs']}, '未解析工序或达到上限；分范围继续检查。')
    add('现场开工条件', mfgorder_name, '未知', '未接入的业务检查',
        '库存、人员资质、设备实时状态、动态采集规则及开工事务前置条件未验证。', '结合现场系统规则核验。')
    out['summary'] = '已完成本次有界静态检查；存在未知或未检查条件，不能据此认定允许开工。'
    out['status_counts'] = {s: sum(c['status'] == s for c in out['checks']) for s in ['通过', '缺失', '未知', '不适用']}
    return encode(out)
