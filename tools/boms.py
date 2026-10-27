"""BOM reads and named-line comparisons; no inventory or demand arithmetic."""
import json

from tools import mcp
from tools.process_context import ReadContext
from tools.modeling_details import raw_json, read_envelope
from tools.resources import reference

NOTE = '标准 BOM 与工单物料清单分别展示，不推断覆盖规则、库存充足或总需求数量。非空 BOM 现场验收待补。'


def output(source):
    return {'source': source, 'complete': False, 'note': NOTE}


def encode(data):
    text = raw_json(data)
    if len(text) > 80000:
        return raw_json({'complete': False, 'error': '结果超过输出上限，请缩小分页；未返回裁剪后的伪完整结果。'})
    return text


def bom_ref(name, revision, instance_id, use_ror=False):
    if use_ror and (revision or instance_id):
        raise ValueError('use_ror 不能与显式版本或 InstanceID 混用。')
    ref = reference(name, instance_id, revision)
    if use_ror:
        ref['UseROR'] = True
    return ref


async def materials(ctx, entity, ref, offset=0, limit=50):
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError('offset >= 0，limit=1..200。')
    obj = await ctx.resolve(entity, ref, revisioned=entity != 'MfgOrders')
    expanded = await ctx.get(entity, obj['InstanceID'], {'$expand': 'MaterialList'})
    if expanded.get('InstanceID') != obj['InstanceID']:
        raise ValueError('展开响应对象 ID 与已解析对象不一致。')
    if entity == 'BOMs' and any(expanded.get(k) != obj.get(k) for k in ('Name', 'Revision')):
        raise ValueError('展开响应的 BOM 名称/版本不一致。')
    view = json.loads(read_envelope(raw_json(expanded), f'{entity}/{obj["InstanceID"]}', offset, limit, ['MaterialList']))
    rows = expanded.get('MaterialList')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        view['complete'] = False
        view['error'] = 'MaterialList 未返回有效的对象列表。'
    return view


@mcp.tool
async def get_bom_details(name: str = '', revision: str = '', instance_id: str | None = None,
                          use_ror: bool = False, offset: int = 0, limit: int = 50) -> str:
    """按 ID、名称+版本或显式 use_ror 读取 BOM 及 MaterialList。保留数量/单位/工序原始字段；分页 complete=false 时不是全量。"""
    out = output('Modeling/BOMs')
    try:
        out.update(await materials(ReadContext(max_requests=4), 'BOMs', bom_ref(name, revision, instance_id, use_ror), offset, limit))
        out['note'] = NOTE
    except ValueError as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def get_product_material_requirements(name: str = '', revision: str = '',
                                            instance_id: str | None = None, use_ror: bool = False,
                                            mfgorder_name: str = '', offset: int = 0, limit: int = 50) -> str:
    """读取产品标准 BOM；可附工单实际物料清单，单独展示不猜覆盖规则。产品须明确版本/ID/ROR；不计算损耗、换算或库存。"""
    out = output('Modeling/Products -> BOMs; optional MfgOrders')
    ctx = ReadContext(max_requests=14)
    if offset < 0 or not 1 <= limit <= 200:
        out['error'] = 'offset >= 0，limit=1..200。'
        return encode(out)
    try:
        product = await ctx.resolve('Products', bom_ref(name, revision, instance_id, use_ror))
        out['product'] = {k: product.get(k) for k in ('InstanceID', 'Name', 'Revision')}
        try:
            out['standard_bom'] = await materials(ctx, 'BOMs', product.get('BOM'), offset, limit)
        except ValueError as error:
            out['standard_bom'] = {'complete': False, 'error': str(error)}
        out['complete'] = out['standard_bom'].get('complete') is True
        if mfgorder_name.strip():
            try:
                order = await ctx.resolve('MfgOrders', reference(mfgorder_name, None), revisioned=False)
                linked = await ctx.resolve('Products', order.get('Product'))
                if linked['InstanceID'] != product['InstanceID']:
                    raise ValueError('工单产品与请求产品版本不一致，未合并物料需求。')
                out['order_materials'] = await materials(ctx, 'MfgOrders', {'InstanceID': order['InstanceID']}, offset, limit)
            except ValueError as error:
                out['order_materials'] = {'complete': False, 'error': str(error)}
            out['complete'] = out['complete'] and out['order_materials'].get('complete') is True
    except ValueError as error:
        out['error'] = str(error)
        out['complete'] = False
    return encode(out)


def named_rows(view):
    if view.get('complete') is not True:
        raise ValueError('物料清单不完整，不能生成完整版本差异；请先核查 BOM 详情与分页。')
    rows = view.get('data', {}).get('MaterialList')
    if not isinstance(rows, list):
        raise ValueError('物料清单缺失。')
    indexed = {}
    for row in rows:
        name = row.get('Name') if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip() or name in indexed:
            raise ValueError('物料明细名称缺失或重复，无法唯一匹配；不按物料名或数组位置猜测。')
        # Named subentity Name is the within-BOM business identity, not its per-version ID.
        indexed[name] = {k: v for k, v in row.items() if k not in {'InstanceID', 'Parent', 'DisplayName'}}
    return indexed


@mcp.tool
async def compare_bom_versions(left_name: str, left_revision: str,
                               right_name: str, right_revision: str) -> str:
    """比较两份明确版本的 BOM 物料清单，按唯一明细 Name 匹配，保留数量/单位/工序差异。上限各200条；缺页或重复名称时不输出误导性增删结果。"""
    out = output('Modeling/BOMs version comparison')
    out['scope'] = 'MaterialList 返回字段；排除行 InstanceID/Parent/DisplayName，不证明全部 BOM 配置等价。改名表现为删除+新增。'
    if not left_revision.strip() or not right_revision.strip():
        out['error'] = '两侧均须明确版本。'
        return encode(out)
    try:
        ctx = ReadContext(max_requests=8)
        left = named_rows(await materials(ctx, 'BOMs', bom_ref(left_name, left_revision, None), limit=200))
        right = named_rows(await materials(ctx, 'BOMs', bom_ref(right_name, right_revision, None), limit=200))
        out.update(complete=True,
                   added=[right[n] for n in sorted(right.keys() - left.keys())],
                   removed=[left[n] for n in sorted(left.keys() - right.keys())],
                   changed=[{'name': n, 'before': left[n], 'after': right[n],
                             'fields': sorted(k for k in left[n].keys() | right[n].keys()
                                              if (k in left[n]) != (k in right[n]) or left[n].get(k) != right[n].get(k))}
                            for n in sorted(left.keys() & right.keys()) if left[n] != right[n]])
    except ValueError as error:
        out['error'] = str(error)
    return encode(out)
