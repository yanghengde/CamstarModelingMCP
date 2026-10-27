"""Fixed read-only difference-reason selections for partial/incremental mode-5 issues."""
import json
from tools import mcp
from tools.boms import encode
from tools.component_issue import get_component_requirement
from tools.component_issue_payload import reason_selection_body, reference
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL


async def selections(requirement, qty):
    body = reason_selection_body(requirement, qty)
    raw = await request('POST', '/api/ComponentIssue/RequestSelectionValues', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params={'selectionValuesExpression': 'ServiceDetails.IssueDifferenceReason', '$top': 100, '$skip': 0},
                        response_formatter=raw_json, retry_read_only=True)
    data = json.loads(raw.split('Response: ', 1)[-1])
    rows = data.get('ServiceDetails')
    if data.get('error') or data.get('ErrorCode') or _has_next_link(data) or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Difference reason selection context is missing, ambiguous or incomplete.')
    table = rows[0].get('IssueDifferenceReason', {})
    values = table.get('SelectionValuesEx')
    if (not isinstance(values, list) or len(values) > 100 or str(table.get('RecordCount')) != str(len(values))
            or str(table.get('RowCount')) != str(len(values))):
        raise ValueError('Difference reason selection is truncated or lacks complete counts.')
    result = []
    for value in values:
        ref = reference(value)
        frozen = value.get('IsFrozen')
        if not (type(frozen) is bool or type(frozen) is str and frozen in ('true', 'false')):
            raise ValueError('Reason frozen state is unknown.')
        result.append({'Name': ref['name'], 'InstanceId': ref['instanceId'],
                       'IsFrozen': frozen is True or frozen == 'true', 'Description': value.get('Description')})
    ids = [r['InstanceId'] for r in result]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate reason identities returned.')
    return result


def choose(values, name):
    matches = [r for r in values if r['Name'] == name]
    if len(matches) != 1 or matches[0]['IsFrozen'] is not False:
        raise ValueError('Explicit difference reason is missing, ambiguous or frozen in this context.')
    return {key: matches[0][key] for key in ('Name', 'InstanceId')}


@mcp.tool
async def get_component_issue_difference_reasons(container_name: str, product_name: str, product_revision: str,
                                                qty: float, container_instance_id: str | None = None) -> str:
    """只读查询模式5部分投料/补投的当前差异原因，最多100条且要求完整；不自动选原因、不执行投料。产品固定版本必填，qty须为不超净剩余且小于原需求的正本次数量。返回Name/ID/冻结状态，准备计划须明确指定原因名称。"""
    try:
        if not all(isinstance(v, str) and v.strip() for v in (container_name, product_name, product_revision)):
            raise ValueError('Container and explicit product revision are required.')
        req = json.loads(await get_component_requirement(container_name, product_name, container_instance_id))
        body = reason_selection_body(req, qty)
        product = body['serviceDetails'][0]['product']
        if product['name'] != product_name.strip() or product['revision'] != product_revision.strip():
            raise ValueError('Product revision mismatch or substitution is unsupported.')
        return encode({'complete': True, 'source': 'ComponentIssue/RequestSelectionValues',
                       'reasons': await selections(req, qty), 'auto_selected': False, 'submitted': False})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return encode({'complete': False, 'submitted': False, 'error': str(error)})
