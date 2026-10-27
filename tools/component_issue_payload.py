"""Strict incremental mode-5 payload; never write cumulative issued quantities."""
import math
import re
from decimal import Decimal
from tools.modeling_details import _has_next_link

PREVIEW_PARAMS = {'execute': 'false', '$select': 'Container,PreventOverIssue',
                  '$expand': 'ServiceDetails($select=BOMLineItem,Product,IssueControl,UOM,QtyRequired,ActualQtyIssued,ActualQty2Issued;$expand=Actuals)'}


def preview_params(body):
    params = dict(PREVIEW_PARAMS)
    if body['serviceDetails'][0].get('issueDifferenceReason'):
        params['$expand'] = params['$expand'].replace('ActualQty2Issued;', 'ActualQty2Issued,IssueDifferenceReason;')
    return params


def quantity(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Quantity must be a finite nonnegative JSON number.')
    return Decimal(str(value))


def reference(value, revisioned=False, parent=False):
    if not isinstance(value, dict):
        raise ValueError('Reference is missing.')
    identifier = value.get('InstanceId') or value.get('InstanceID')
    if (not isinstance(identifier, str) or not re.fullmatch('[0-9a-fA-F]{16}', identifier)
            or not isinstance(value.get('Name'), str) or not value['Name'].strip()):
        raise ValueError('Reference needs an explicit name and 16-digit identity.')
    out = {'name': value['Name'], 'instanceId': identifier}
    if revisioned:
        ror = value.get('UseROR', value.get('UseRor'))
        if not isinstance(value.get('Revision'), str) or not value['Revision'] or not (ror is None or ror is False):
            raise ValueError('An explicit fixed product revision is required.')
        out['revision'] = value['Revision']
    if parent:
        owner = value.get('Parent')
        identifier = (owner.get('InstanceId') or owner.get('InstanceID')) if isinstance(owner, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-fA-F]{16}', identifier):
            raise ValueError('Material line parent identity is required.')
        out['parent'] = {'instanceId': identifier}
    return out


def _build_quantity_issue(requirement, qty, difference_reason=None, selection_only=False):
    if requirement.get('status') != 'matched' or requirement.get('complete') is not True:
        raise ValueError('A complete matched requirement is required.')
    data = requirement['data']; row = data['IssueDetails']
    requested = quantity(qty)
    if type(row.get('IssueControl')) is not int or row['IssueControl'] != 5:
        raise ValueError('Only quantity-only mode 5 is supported.')
    required = quantity(row.get('QtyRequired'))
    issued = quantity(row.get('QtyIssued'))
    pending = quantity(row.get('ActualQtyIssued'))
    remaining = quantity(row.get('NetQtyRequired'))
    if pending != 0 or issued > required or remaining != required-issued-pending:
        raise ValueError('Demand balance is inconsistent, contains pending actuals, or exceeds supported requirements.')
    if row.get('RemovalHistoryDetails') != []:
        raise ValueError('Issues require an explicitly empty removal history; return/netting rules are unsupported.')
    if requested <= 0 or requested > remaining:
        raise ValueError('Requested increment must be positive and must not exceed current net requirement.')
    if requested < required and difference_reason is None and not selection_only:
        raise ValueError('Partial issue requires an explicitly selected difference reason.')
    if requested == required and difference_reason is not None:
        raise ValueError('Full-demand issue does not accept an unused difference reason.')
    if any(quantity(row.get(key)) != 0 for key in ('Qty2Required', 'NetQty2Required', 'Qty2Issued', 'ActualQty2Issued')):
        raise ValueError('Secondary quantities are outside this narrow contract.')
    if type(row.get('ValidateQtyDifference')) is not bool:
        raise ValueError('Quantity difference rule is unknown.')
    target = reference(data['ParentContainer'])
    product = reference(row['Product'], revisioned=True)
    line = reference(row['BOMLineItem'], parent=True)
    unit = reference(row['UOM'])
    actual = {'product': product, 'toContainer': target, 'qtyIssued': qty, 'qty2Issued': 0}
    result = {'container': target, 'preventOverIssue': True, 'serviceDetails': [{
        'bomLineItem': line, 'product': product, 'issueControl': 5, 'uom': unit,
        'qtyRequired': row['QtyRequired'], 'actualQtyIssued': qty, 'actualQty2Issued': 0,
        'actuals': [actual]}]}
    if difference_reason is not None:
        reason = reference(difference_reason)
        result['serviceDetails'][0]['issueDifferenceReason'] = reason
        actual['issueDifferenceReason'] = reason
    return result


def build_quantity_issue(requirement, qty, difference_reason=None):
    return _build_quantity_issue(requirement, qty, difference_reason)


def reason_selection_body(requirement, qty):
    """Read-only selection context; no reason is guessed or selected here."""
    required = requirement['data']['IssueDetails']['QtyRequired']
    if not 0 < quantity(qty) < quantity(required):
        raise ValueError('Difference reason selection requires a positive increment smaller than the original requirement.')
    return _build_quantity_issue(requirement, qty, selection_only=True)


def quantity_context(snapshot):
    """Review-only original balance and incremental intent, never a server field."""
    row = snapshot['constraints']; issued = quantity(row['QtyIssued'])
    remaining = quantity(row['NetQtyRequired'])
    requested = quantity(snapshot['body']['serviceDetails'][0]['actualQtyIssued'])
    return {'scope': 'incremental_issue' if issued > 0 else 'first_issue',
            'required': str(quantity(row['QtyRequired'])), 'cumulative_issued_before': str(issued),
            'net_required_before': str(remaining), 'quantity_this_request': str(requested),
            'projected_cumulative_issued': str(issued+requested), 'projected_net_required': str(remaining-requested),
            'note': '预测仅供审阅；实际提交及历史仍须由服务端校验，不把累计量写回请求。'}


def verify_quantity_echo(response, body):
    """Only serialization proof, not execution or eligibility proof."""
    data = response.get('data', response)
    if response.get('error') or response.get('ErrorCode') or not isinstance(data, dict) or _has_next_link(response):
        raise ValueError('Service preview returned an error.')

    def echo_ref(value, revisioned=False, parent=False):
        if isinstance(value, dict):
            value = dict(value)
            ror = value.get('UseROR', value.get('UseRor'))
            if ror == 'false':
                value['UseROR'] = False
        return reference(value, revisioned, parent)

    def echo_number(value):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError('Invalid echoed quantity.')
        try:
            parsed = Decimal(str(value))
        except ArithmeticError:
            raise ValueError('Invalid echoed quantity.') from None
        if not parsed.is_finite() or parsed < 0:
            raise ValueError('Invalid echoed quantity.')
        return parsed

    flag = data.get('PreventOverIssue')
    if echo_ref(data.get('Container')) != body['container'] or not (flag is True or type(flag) is str and flag == 'true'):
        raise ValueError('Target or over-issue prevention changed.')
    rows = data.get('ServiceDetails')
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Exactly one echoed requirement is required.')
    row = rows[0]; expected = body['serviceDetails'][0]
    for field, key, rev, parent in [('Product', 'product', True, False), ('BOMLineItem', 'bomLineItem', False, True), ('UOM', 'uom', False, False)]:
        if echo_ref(row.get(field), rev, parent) != expected[key]:
            raise ValueError('Requirement reference changed: ' + field)
    for field, key in [('QtyRequired', 'qtyRequired'), ('ActualQtyIssued', 'actualQtyIssued'), ('ActualQty2Issued', 'actualQty2Issued'), ('IssueControl', 'issueControl')]:
        if echo_number(row.get(field)) != Decimal(str(expected[key])):
            raise ValueError('Requirement amount/mode changed: ' + field)
    actuals = row.get('Actuals')
    if not isinstance(actuals, list) or len(actuals) != 1:
        raise ValueError('Exactly one nested actual is required.')
    actual = actuals[0]; expected = expected['actuals'][0]
    if (echo_ref(actual.get('Product'), True) != expected['product']
            or echo_ref(actual.get('ToContainer')) != expected['toContainer']
            or echo_number(actual.get('QtyIssued')) != Decimal(str(expected['qtyIssued']))
            or echo_number(actual.get('Qty2Issued')) != 0):
        raise ValueError('Nested actual product, target or quantity changed.')
    reason = body['serviceDetails'][0].get('issueDifferenceReason')
    if reason is not None:
        if echo_ref(row.get('IssueDifferenceReason')) != reason or echo_ref(actual.get('IssueDifferenceReason')) != reason:
            raise ValueError('Selected difference reason was not echoed at both requirement and actual levels.')
    return True
