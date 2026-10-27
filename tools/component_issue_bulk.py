"""Mode-2 source checks and serialization preview only; no executable plan."""
import asyncio
import json
from typing import Annotated

from pydantic import Field

from tools import mcp
from tools.boms import encode
from tools.component_issue import get_component_requirement
from tools.component_issue_payload import reference, quantity
from tools.component_issue_receipts import identifier, number
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL


def checked_status(result, name, explicit_id=None):
    row = result.get('data', {}).get('ContainerStatusDetails', {})
    ref = reference(row.get('Container'))
    if (result.get('complete') is not True or result.get('error') or _has_next_link(result)
            or ref['name'] != name or explicit_id and ref['instanceId'] != explicit_id):
        raise ValueError('容器身份或完整状态未确认。')
    if type(row.get('Status')) is not int or row['Status'] != 1 or row.get('InProcess') is not True:
        raise ValueError('当前受限预览要求活动、在制容器。')
    if row.get('InRework') is not False:
        raise ValueError('返工状态未知或非正常状态，超出当前预览范围。')
    return row, ref


def build_preview(target, source, requirement, source_balance, children, qty):
    """Check one untouched, full-demand mode-2 line against one leaf source."""
    requested = quantity(qty)
    if requested <= 0:
        raise ValueError('本次数量必须为有限正数。')
    target_ref = reference(target['Container'])
    source_ref = reference(source['Container'])
    if source_ref['instanceId'] == target_ref['instanceId'] or source_ref['name'] == target_ref['name']:
        raise ValueError('来源与目标必须是不同容器。')
    if requirement.get('complete') is not True or requirement.get('status') != 'matched' or _has_next_link(requirement):
        raise ValueError('未取得完整单条物料需求。')
    data = requirement['data']; row = data['IssueDetails']
    if reference(data['ParentContainer']) != target_ref or data.get('ObjectName') != source_ref['name']:
        raise ValueError('需求必须通过明确来源容器匹配到当前目标。')
    if type(row.get('IssueControl')) is not int or row['IssueControl'] != 2:
        raise ValueError('当前来源预览仅支持明确模式2，不转换其他模式。')
    required = quantity(row.get('QtyRequired'))
    if (required != requested or quantity(row.get('NetQtyRequired')) != required
            or quantity(row.get('QtyIssued')) != 0 or quantity(row.get('ActualQtyIssued')) != 0):
        raise ValueError('当前仅支持未消耗需求的完整首次投料；部分量/补投待单独验证。')
    if (row.get('RemovalHistoryDetails') != [] or type(row.get('ValidateQtyDifference')) is not bool
            or any(quantity(row.get(k)) != 0 for k in ('Qty2Required', 'NetQty2Required', 'Qty2Issued', 'ActualQty2Issued'))):
        raise ValueError('退料、未知差异规则或第二数量超出当前来源预览范围。')
    product = reference(row['Product'], revisioned=True)
    if reference(source.get('Product'), revisioned=True) != product:
        raise ValueError('来源产品身份/固定版本与需求不匹配，不推断替代关系。')
    line = reference(row['BOMLineItem'], parent=True)
    order = reference(target.get('MfgOrder'))
    if line['parent']['instanceId'] != order['instanceId']:
        raise ValueError('物料需求行不属于目标当前工单。')
    unit = reference(row['UOM'])
    if reference(source.get('UOM')) != unit:
        raise ValueError('来源与需求单位不匹配，不做单位换算。')
    reference(target.get('Spec'), revisioned=True)
    reference(target.get('Operation')); reference(target.get('Step'))
    balance = source_balance
    if (balance.get('ContainerId') != source_ref['instanceId'] or balance.get('ContainerName') != source_ref['name']
            or balance.get('ProductId') != product['instanceId'] or balance.get('UOMId') != unit['instanceId']
            or isinstance(balance.get('Status'), bool) or balance.get('Status') not in (1, '1')):
        raise ValueError('来源余额与状态/产品/单位身份不一致。')
    available = number(balance.get('Qty'))
    if available != quantity(source.get('Qty')) or requested > available:
        raise ValueError('来源数量不一致或不足；逐次读取不是锁定的可用库存。')
    # The fixed query retains explicit empty/null columns, unlike compact Inquiry.
    if (any(k not in balance for k in ('Qty2', 'UOM2Id', 'ParentContainerId'))
            or balance['Qty2'] not in ('', None, 0, '0') or isinstance(balance['Qty2'], bool)
            or balance['UOM2Id'] not in ('', None) or balance['ParentContainerId'] not in ('', None)):
        raise ValueError('来源第二数量/第二单位或父关系不明确或超出当前范围。')
    if not isinstance(children, list) or children:
        raise ValueError('来源必须是已核对无当前子容器的根叶子容器。')
    body = {'container': target_ref, 'preventOverIssue': True, 'serviceDetails': [{
        'bomLineItem': line, 'product': product, 'uom': unit, 'issueControl': 2,
        'qtyRequired': row['QtyRequired'], 'actualQtyIssued': qty, 'actualQty2Issued': 0,
        'actuals': [{'product': product, 'toContainer': target_ref, 'qtyIssued': qty, 'qty2Issued': 0}]}],
        'issueActualDetails': [{'bomLineItem': line, 'fromContainer': source_ref, 'toContainer': target_ref,
                                'product': product, 'qtyIssued': qty, 'qty2Issued': 0}]}
    return body, {'available_observed': str(available), 'quantity_this_request': str(requested),
                  'projected_source_quantity': str(available-requested), 'unit': unit,
                  'note': '预测不是库存预留；未提交事务，不证明未冻结/可被现场消费。'}


BULK_PREVIEW_PARAMS = {
    'execute': 'false', '$select': 'Container,PreventOverIssue',
    '$expand': 'ServiceDetails($select=BOMLineItem,Product,IssueControl,UOM,QtyRequired,ActualQtyIssued,ActualQty2Issued;'
               '$expand=Actuals($select=Product,ToContainer,QtyIssued,Qty2Issued)),'
               'IssueActualDetails($select=BOMLineItem,FromContainer,ToContainer,Product,QtyIssued,Qty2Issued)'}

DERIVED_PREVIEW_PARAMS = {
    'execute': 'false', '$select': 'Container,PreventOverIssue',
    '$expand': 'ServiceDetails($select=BOMLineItem,Product,IssueControl,UOM,QtyRequired,ActualQtyIssued,ActualQty2Issued;'
               '$expand=Actuals)'}


def derived_request(body):
    """Use the live EDM subtype, with exactly one source-bearing actual list."""
    import copy
    result = copy.deepcopy(body)
    source = result.pop('issueActualDetails')[0]['fromContainer']
    line = result['serviceDetails'][0]
    line['@odata.type'] = '#shopfloor.IssueDetailsBulk'
    line['actuals'][0].update({'@odata.type': '#shopfloor.ActualsBulk', 'fromContainer': source})
    return result


def verify_echo(data, body):
    if not isinstance(data, dict) or data.get('error') or data.get('ErrorCode') or _has_next_link(data):
        raise ValueError('非执行回显错误或存在后续页。')
    data = data.get('data', data)
    if (not isinstance(data, dict) or reference(data.get('Container')) != body['container']
            or not (data.get('PreventOverIssue') is True or data.get('PreventOverIssue') == 'true')):
        raise ValueError('非执行回显未确认目标或防超量标志。')
    sections = [('ServiceDetails', 'serviceDetails')]
    if 'issueActualDetails' in body:
        sections.append(('IssueActualDetails', 'issueActualDetails'))
    for section, key in sections:
        rows = data.get(section)
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('非执行回显必须含唯一需求及唯一来源实际明细。')
        actual, expected = rows[0], body[key][0]
        for field, value in expected.items():
            if field == '@odata.type':
                # Camstar's serializer returns CDOTypeId on nested actuals;
                # it does not echo OData annotations. Check that ID below.
                continue
            label = {'bomLineItem': 'BOMLineItem', 'uom': 'UOM'}.get(field, field[0].upper()+field[1:])
            if field == 'actuals':
                nested = actual.get('Actuals')
                if not isinstance(nested, list) or len(nested) != 1:
                    raise ValueError('需求行实际量回显不完整。')
                child = nested[0]; planned = value[0]
                product = child.get('Product')
                if isinstance(product, dict) and product.get('UseROR', product.get('UseRor')) == 'false':
                    product = {**product, 'UseROR': False}
                if (reference(product, revisioned=True) != planned['product']
                        or reference(child.get('ToContainer')) != planned['toContainer']
                        or number(child.get('QtyIssued')) != quantity(planned['qtyIssued'])
                        or number(child.get('Qty2Issued')) != 0):
                    raise ValueError('需求行实际量/产品/目标回显不匹配。')
                if 'fromContainer' in planned and (
                        reference(child.get('FromContainer')) != planned['fromContainer']
                        or child.get('CDOTypeId') not in ('6540', 6540)):
                    raise ValueError('ActualsBulk派生类型或来源回显不匹配。')
            elif isinstance(value, dict):
                echoed = actual.get(label)
                if isinstance(echoed, dict) and echoed.get('UseROR', echoed.get('UseRor')) == 'false':
                    echoed = {**echoed, 'UseROR': False}
                if reference(echoed, revisioned=field == 'product', parent=field == 'bomLineItem') != value:
                    raise ValueError('非执行回显引用不匹配：'+label)
            elif number(actual.get(label)) != quantity(value):
                raise ValueError('非执行回显数量/模式不匹配：'+label)


@mcp.tool
async def preview_bulk_component_issue(container_name: str, source_container_name: str, product_name: str,
        product_revision: str, qty: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)],
        container_instance_id: str | None = None, source_container_instance_id: str | None = None,
        verify_serialization: bool = False) -> str:
    """模式2来源容器只读校验，不生成执行计划、不扣库存。仅首次完整需求、固定版本/同单位、根叶子来源、无第二数量/退料。可选execute=false核对IssueDetailsBulk/ActualsBulk派生类型、来源及数量，executable恒false。正式单次计划使用prepare_bulk_component_issue。"""
    out = {'executable': False, 'complete': False, 'submitted': False, 'issue_control': 2,
           'source_checks_passed': False, 'serialization_verified': False,
           'blockers': ['来源冻结、现场可消费性及其他业务规则未验收。',
                        '使用实时EDM派生类型，单组ActualsBulk记录来源；F隔离单次扣减/历史已实测，不能视为通用执行许可。',
                        '此预览不是计划；正式提交必须prepare_bulk_component_issue并确认相同计划ID/摘要。'],
           'note': '当前读取不是一致性快照或库存预留；不执行投料，不转换为模式5。'}
    async def inspect():
        for name in (container_name, source_container_name, product_name, product_revision):
            if not isinstance(name, str) or not name.strip():
                raise ValueError('明确目标、来源和产品固定版本必填。')
        quantity(qty)
        if qty <= 0 or type(verify_serialization) is not bool:
            raise ValueError('数量必须为有限正数；verify_serialization必须为布尔值。')
        for value in (container_instance_id, source_container_instance_id):
            if value is not None:
                identifier(value)
        target_result = json.loads(await get_container_details(container_name.strip(), container_instance_id))
        source_result = json.loads(await get_container_details(source_container_name.strip(), source_container_instance_id))
        target, target_ref = checked_status(target_result, container_name.strip(), container_instance_id)
        source, source_ref = checked_status(source_result, source_container_name.strip(), source_container_instance_id)
        if source_ref['instanceId'] == target_ref['instanceId']:
            raise ValueError('来源不能是目标自身。')
        requirement = json.loads(await get_component_requirement(target_ref['name'], source_ref['name'], target_ref['instanceId']))
        out.update(target_status=target_result, source_status=source_result, requirement=requirement)
        sid = identifier(source_ref['instanceId'])
        async def rows(query):
            raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                                body={'queryText': query}, response_formatter=raw_json, retry_read_only=True)
            result = json.loads(raw)
            if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
                raise ValueError('来源固定查询未返回记录数组。')
            return result
        balances = await rows('SELECT TOP 2 ContainerId,ContainerName,ProductId,Qty,Qty2,UOMId,UOM2Id,Status,ParentContainerId '
                              f"FROM Container WHERE ContainerId='{sid}'")
        if len(balances) != 1:
            raise ValueError('来源余额记录缺失或不唯一。')
        children = await rows(f"SELECT TOP 1 ContainerId FROM Container WHERE ParentContainerId='{sid}'")
        out.update(source_balance=balances[0], current_children=children)
        body, balance = build_preview(target, source, requirement, balances[0], children, qty)
        body = derived_request(body)
        product = body['serviceDetails'][0]['product']
        if product['name'] != product_name.strip() or product['revision'] != product_revision.strip():
            raise ValueError('需求/来源产品与明确输入版本不匹配。')
        out.update(source_checks_passed=True, stage='source_checked', quantity_context=balance, request_preview=body)
        if verify_serialization:
            raw = await request('POST', '/api/ComponentIssue', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                                body=body, params=dict(DERIVED_PREVIEW_PARAMS), response_formatter=raw_json)
            data = json.loads(raw.split('Response: ', 1)[-1])
            out['service_echo'] = data
            verify_echo(data, body)
            out.update(serialization_verified=True, stage='serialization_preview')
    try:
        await asyncio.wait_for(inspect(), 90)
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        out['error'] = str(error) or '模式2来源预览超时。'
    return encode(out)
