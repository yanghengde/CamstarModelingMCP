"""Reviewable issue intent; not an executable Shopfloor payload."""
import math
import re
from decimal import Decimal
from typing import Literal
from tools import mcp
from tools.boms import encode
from tools.process_context import ReadContext
from tools.resources import reference
from tools.container_inquiry import get_container_details
import json
from config import CAMSTAR_QUERY_BASE_URL
from core.http_client import request
from tools.modeling_details import raw_json, _has_next_link


def assess_quantity_requirement(requirement, product, quantity):
    """Compare mode-5 intent with observed demand; never grants execution permission."""
    checks = []

    def check(name, status, reason):
        checks.append({'check': name, 'status': status, 'reason': reason})

    def identity(value):
        identifier = (value.get('InstanceID') or value.get('InstanceId')) if isinstance(value, dict) else None
        return identifier if isinstance(identifier, str) and re.fullmatch(r'[0-9a-fA-F]{16}', identifier) else None

    def amount(value):
        return Decimal(str(value)) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

    if requirement.get('status') != 'matched' or requirement.get('complete') is not True:
        check('requirement', 'unknown', '未取得完整单条需求，无法核对数量模式。')
        return {'checks': checks, 'observed_checks_passed': False, 'execution_authorized': False}
    detail = requirement.get('data', {}).get('IssueDetails')
    if not isinstance(detail, dict):
        check('requirement', 'unknown', '需求明细不是对象。')
        return {'checks': checks, 'observed_checks_passed': False, 'execution_authorized': False}
    actual = detail.get('Product')
    if not identity(actual) or not identity(product) or not isinstance(actual, dict) or not actual.get('Revision'):
        check('product_version', 'unknown', '需求或目标产品缺少明确身份/版本。')
    else:
        ror = actual.get('UseROR', actual.get('UseRor'))
        same = (identity(actual) == identity(product) and actual.get('Name') == product.get('Name')
                and actual['Revision'] == product.get('Revision')
                and (ror is None or ror is False))
        check('product_version', 'pass' if same else 'blocked', '产品身份及固定版本匹配。' if same else '产品身份/版本不匹配或需求使用ROR；尚不支持替代料判断。')
    mode = detail.get('IssueControl')
    check('issue_control', 'pass' if type(mode) is int and mode == 5 else 'unknown' if mode is None else 'blocked',
          '需求必须明确为模式5（仅数量）。')
    line = detail.get('BOMLineItem')
    line_ok = identity(line) and identity(line.get('Parent'))
    check('bom_line', 'pass' if line_ok else 'unknown', '需求行及所属对象必须有明确ID。')
    unit = detail.get('UOM')
    unit_ok = identity(unit) and isinstance(unit.get('Name'), str) and unit['Name'].strip()
    check('unit', 'pass' if unit_ok else 'unknown', '本次数量采用需求单位，不进行单位换算。')
    required, remaining = amount(detail.get('QtyRequired')), amount(detail.get('NetQtyRequired'))
    requested = amount(quantity)
    if required is None or remaining is None or requested is None:
        check('remaining_quantity', 'unknown', '需求量或净剩余量缺失/无效，不用累计量推算。')
    else:
        check('remaining_quantity', 'pass' if 0 < requested <= remaining else 'blocked',
              '本次数量在已返回的净剩余需求范围内。' if 0 < requested <= remaining else '本次数量为零或超过净剩余需求。')
    variance_rule = detail.get('ValidateQtyDifference')
    if type(variance_rule) is not bool or requested is None or remaining is None:
        check('quantity_difference', 'unknown', '未能确认数量差异校验规则或净剩余量。')
    elif variance_rule and requested != remaining:
        check('quantity_difference', 'blocked', '现场要求数量差异原因；当前预览未提供差异原因，不支持部分需求投料。')
    else:
        check('quantity_difference', 'pass', '本次数量未触发已返回的数量差异要求；实际执行仍由服务端校验。')
    secondary = [amount(detail.get(key)) for key in ('Qty2Required', 'NetQty2Required')]
    check('secondary_quantity', 'unknown' if any(value is None for value in secondary) else 'pass' if all(value == 0 for value in secondary) else 'blocked',
          '当前预览没有第二数量输入，要求明确的第二需求量均为0。')
    # The schema describes this as a validation result, not a configured prohibition.
    check('assembly_sequence', 'unknown',
          'ValidSequence仅表示序列是否已验证；当前值=' + str(detail.get('ValidSequence')) + '，仍需确认IssuePoint/序列的现场执行规则。')
    return {'checks': checks, 'observed_checks_passed': all(c['status'] == 'pass' for c in checks),
            'execution_authorized': False, 'quantity_unit': unit,
            'net_quantity_required': detail.get('NetQtyRequired')}


@mcp.tool
async def get_component_issue_history(container_name: str, container_instance_id: str | None = None,
                                      offset: int = 0, limit: int = 20) -> str:
    """固定只读投料实际历史查询，按 ToContainerId 定位直接接收容器，返回原始证据ID/来源/数量。不是净消耗、完整族谱或某次请求成功证明；分页limit=1..100。"""
    out = {'complete': False, 'source': 'Query/AdHoc IssueActualsHistory',
           'scope': 'direct_to_container_actual_issues',
           'note': '不扣除移除/退料，不推断净消耗；ID排序不是时间排序，并发变化时分页非一致快照。空页不能证明某次请求失败或安全重试。'}
    try:
        if not container_name.strip() or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('容器名必填；offset为非负整数，limit=1..100。')
        info = json.loads(await get_container_details(container_name, container_instance_id, view='info'))
        ref = info.get('data', {}).get('Container')
        identifier = (ref.get('InstanceId') or ref.get('InstanceID')) if isinstance(ref, dict) else None
        if (info.get('error') or not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', identifier)
                or ref.get('Name') != container_name.strip() or (container_instance_id is not None and identifier != container_instance_id.strip())):
            raise ValueError('无法核对目标容器名称与16位ID，未执行历史查询。')
        query = ('SELECT IssueActualsHistoryId,IssueHistoryDetailId,HistoryId,TxnId,ToContainerId,'
                 'FromContainerId,FromLot,FromStockPoint,ProductId,Qty,Qty2 FROM IssueActualsHistory '
                 f"WHERE ToContainerId='{identifier}' ORDER BY IssueActualsHistoryId "
                 f'OFFSET {offset} ROWS FETCH NEXT {limit + 1} ROWS ONLY')
        raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                            body={'queryText': query}, response_formatter=raw_json, retry_read_only=True)
        rows = json.loads(raw)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get('IssueActualsHistoryId')
                                          or row.get('ToContainerId') != identifier for row in rows):
            raise ValueError('历史响应非有效记录列表或包含其他目标容器。')
        more = len(rows) > limit
        out.update(container=ref, rows=rows[:limit], has_more=more, next_offset=offset + limit if more else None,
                   offset=offset, page_complete=True, complete=offset == 0 and not more)
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def get_component_requirement(container_name: str, object_name: str,
                                     container_instance_id: str | None = None) -> str:
    """固定只读 ComponentIssueInquiry，parentContainer+objectName 匹配产品/替代产品/来源容器的需求。返回单条IssueDetails，不是完整BOM或消耗历史；不执行投料。"""
    out = {'source': 'Query/ComponentIssueInquiry', 'complete': False, 'status': 'unknown',
           'note': '单条需求匹配，不是全部需求或本次实际消耗；QtyIssued 为累计量。'}
    if not container_name.strip() or not object_name.strip() or (container_instance_id is not None and not container_instance_id.strip()):
        out['error'] = '目标容器、匹配对象必填，显式ID不能为空。'
        return encode(out)
    ref = {'name': container_name.strip()}
    if container_instance_id:
        ref['instanceId'] = container_instance_id.strip()
    raw = await request('POST', '/api/ComponentIssueInquiry', base_url=CAMSTAR_QUERY_BASE_URL,
                        body={'parentContainer': ref, 'objectName': object_name.strip()},
                        params={'execute': 'true', '$select': 'ParentContainer,ObjectName', '$expand': 'IssueDetails'},
                        response_formatter=raw_json, retry_read_only=True)
    try:
        data = json.loads(raw.split('Response: ', 1)[-1])
        if not isinstance(data, dict):
            raise ValueError('需求查询未返回对象。')
        out['data'] = data
        if data.get('ErrorCode') or data.get('error'):
            out['status'] = 'no_matching_requirement' if str(data.get('ErrorCode')) == '13501228' else 'query_failed'
            out['error'] = data
        else:
            parent = data.get('ParentContainer')
            identifier = (parent or {}).get('InstanceId') or (parent or {}).get('InstanceID') if isinstance(parent, dict) else None
            if not isinstance(parent, dict) or parent.get('Name') != container_name.strip() or not identifier:
                raise ValueError('响应未确认目标父容器身份。')
            if container_instance_id and identifier != container_instance_id.strip():
                raise ValueError('响应父容器ID不匹配。')
            if data.get('ObjectName') != object_name.strip():
                raise ValueError('响应匹配对象与请求不一致。')
            detail = data.get('IssueDetails')
            out['data'] = data
            required = detail.get('QtyRequired') if isinstance(detail,dict) else None
            if (isinstance(detail,dict) and isinstance(detail.get('BOMLineItem'),dict) and detail['BOMLineItem']
                    and type(required) in (int,float) and math.isfinite(required) and required >= 0):
                out['status'] = 'matched'
                out['complete'] = not _has_next_link(data)
            else:
                out['status'] = 'requirement_incomplete'
    except (ValueError, TypeError) as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def preview_component_issue(container_name: str, product_name: str, product_revision: str,
                                   qty: float, issue_control: Literal[1, 2, 3, 4, 5],
                                   source_name: str = '', container_instance_id: str | None = None) -> str:
    """仅预览单条投料意图，不执行、不生成可提交payload。模式1/2来源容器，3批号，4库存点，5数量；数量正数、产品明确版本。LoadRequirements/单位/现场状态尚未验证，executable始终false。"""
    out = {'executable': False, 'complete': False, 'stage': 'intent_preview',
           'blockers': ['尚未验证 LoadRequirements 需求加载契约与 BOM 行定位。',
                        '尚未核对单位、允许投料数量、工序及当前现场状态。',
                        '尚未验证事务成功回读与重复提交核实路径。'],
           'note': '本预览不执行投料，不证明物料满足需求，不是可提交的 Shopfloor 请求。'}
    try:
        if (not container_name.strip() or not product_name.strip() or not product_revision.strip()
                or isinstance(qty, bool) or not math.isfinite(qty) or qty <= 0
                or type(issue_control) is not int or issue_control not in (1, 2, 3, 4, 5)):
            raise ValueError('名称/产品版本必填，数量须为有限正数，issue_control=1..5。')
        if issue_control != 5 and not source_name.strip():
            raise ValueError('此追踪模式必须明确来源容器、批号或库存点。')
        if issue_control == 5 and source_name.strip():
            raise ValueError('仅数量模式不接受来源，避免来源被忽略。')
        if container_instance_id is not None and not container_instance_id.strip():
            raise ValueError('容器 ID 不能为空字符串。')
        out['intent'] = {'target': container_name.strip(), 'product': product_name.strip(),
                         'revision': product_revision.strip(), 'quantity_to_issue': qty,
                         'issue_control': issue_control, 'source': source_name.strip() or None,
                         'source_kind': {1: 'serial_container', 2: 'bulk_container', 3: 'lot', 4: 'stock_point', 5: 'quantity_only'}[issue_control]}
        product = await ReadContext(max_requests=2).resolve('Products', reference(product_name, None, product_revision))
        out['resolved_product'] = {k: product.get(k) for k in ['InstanceID', 'Name', 'Revision']}
        info = json.loads(await get_container_details(container_name, container_instance_id, view='info'))
        ref = info.get('data', {}).get('Container')
        if info.get('error') or not isinstance(ref, dict) or ref.get('Name') != container_name.strip():
            raise ValueError('目标容器身份无法核对。')
        identifier = ref.get('InstanceId') or ref.get('InstanceID')
        if not identifier or (container_instance_id and identifier != container_instance_id.strip()):
            raise ValueError('目标容器 ID 无法核对。')
        out['resolved_target'] = ref
        out['target_query_complete'] = info.get('complete') is True
        out['requirement_match'] = json.loads(await get_component_requirement(
            container_name, source_name if issue_control in (1, 2) else product_name, identifier))
        if out['requirement_match'].get('status') != 'matched':
            out['blockers'].append('未获得可核实的目标物料需求匹配，不能执行投料。')
        if issue_control != 5:
            out['blockers'].append('来源仅记录为用户意图，尚未核对其存在性、余额及与需求的匹配。')
        else:
            out['quantity_requirement_checks'] = assess_quantity_requirement(
                out['requirement_match'], product, qty)
            out['blockers'].extend(c['reason'] for c in out['quantity_requirement_checks']['checks']
                                   if c['status'] != 'pass')
            out['blockers'].append('数量模式仍须核对需求单位、工序允许条件及实际记录规则。')
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)
