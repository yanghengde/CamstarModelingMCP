"""Fixed, bounded read-only receipt chain for single-line quantity-only issues."""
import json
import re
from decimal import Decimal, InvalidOperation

from config import CAMSTAR_QUERY_BASE_URL
from core.http_client import request
from tools.modeling_details import raw_json
from tools.component_issue_payload import build_quantity_issue


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{16}', value):
        raise ValueError('Receipt reference must be an explicit 16-digit ID.')
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('History quantity is missing or not numeric.')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid history quantity.') from None
    if not result.is_finite() or result < 0:
        raise ValueError('History quantity must be finite and nonnegative.')
    return result


async def receipt(payload):
    """No writes or state changes. Missing/ambiguous evidence never proves failure."""
    evidence = {'complete': False, 'verified_success': False, 'reads': [],
                'source': 'HistoryMainline -> IssueHistoryDetail -> ComponentIssueHistory + IssueActualsHistory'}
    try:
        snap = payload['snapshot']; body = snap['body']; line = body['serviceDetails'][0]
        reconstructed = build_quantity_issue({'status': 'matched', 'complete': True, 'data': {
            'ParentContainer': snap['context']['Container'], 'IssueDetails': snap['constraints']}}, snap['arguments']['qty'], snap.get('difference_reason'))
        if reconstructed != body:
            raise ValueError('Stored request is outside the validated narrow mode-5 contract.')
        target = identifier(body['container']['instanceId'])
        product = identifier(line['product']['instanceId'])
        material = identifier(line['bomLineItem']['instanceId'])
        unit = identifier(line['uom']['instanceId'])
        spec = identifier(snap['context']['Spec']['InstanceId'])
        operation = identifier(snap['context']['Operation']['InstanceId'])
        marker = payload['marker']
        if not isinstance(marker, str) or not re.fullmatch('Codex issue [0-9a-f]{32}', marker):
            raise ValueError('Unknown audit marker format; no query sent.')
        if (type(line.get('issueControl')) is not int or line['issueControl'] != 5
                or len(body['serviceDetails']) != 1 or len(line['actuals']) != 1):
            raise ValueError('Receipt supports only single-line, single-actual mode 5.')
        qty = number(line['actualQtyIssued'])
        reason = identifier(line['issueDifferenceReason']['instanceId']) if line.get('issueDifferenceReason') else None
        if qty <= 0 or number(line['actualQty2Issued']) != 0:
            raise ValueError('Receipt supports positive primary and zero secondary quantity only.')

        async def one(label, query):
            raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                                body={'queryText': query}, response_formatter=raw_json, retry_read_only=True)
            evidence['reads'].append({'table': label, 'query': query, 'response': raw})
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError(label+': expected exactly one record; empty/duplicate/error is inconclusive.')
            return rows[0]

        main = await one('HistoryMainline',
            'SELECT TOP 2 HistoryMainlineId,TxnId,ContainerId,TxnTypeName,SpecId,OperationId,Comments '
            f"FROM HistoryMainline WHERE ContainerId='{target}' AND Comments='{marker}'")
        main_id, txn = identifier(main.get('HistoryMainlineId')), identifier(main.get('TxnId'))
        if (main.get('ContainerId') != target or main.get('Comments') != marker
                or main.get('TxnTypeName') != 'ComponentIssue' or main.get('SpecId') != spec
                or main.get('OperationId') != operation):
            raise ValueError('Mainline marker/type/container/spec/operation mismatch.')
        detail = await one('IssueHistoryDetail',
            'SELECT TOP 2 IssueHistoryDetailId,ComponentIssueHistoryId,TxnId,IntegrationTxnId,HistoryId,'
            'ProductMaterialListItemId,ProductId,IssueControl,UOMId,QtyRequired,Qty2Required,ActualQtyIssued,ActualQty2Issued '
            + (',IssueDifferenceReasonId ' if reason else '') +
            f"FROM IssueHistoryDetail WHERE TxnId='{txn}'")
        detail_id = identifier(detail.get('IssueHistoryDetailId'))
        header_id = identifier(detail.get('ComponentIssueHistoryId'))
        if (detail.get('TxnId') != txn or detail.get('IntegrationTxnId') != main_id or detail.get('HistoryId') != target
                or detail.get('ProductMaterialListItemId') != material or detail.get('ProductId') != product
                or detail.get('UOMId') != unit or number(detail.get('IssueControl')) != 5
                or number(detail.get('ActualQtyIssued')) != qty or number(detail.get('ActualQty2Issued')) != 0
                or number(detail.get('QtyRequired')) != number(line['qtyRequired'])
                or number(detail.get('Qty2Required')) != 0):
            raise ValueError('Demand line/transaction/unit/product/quantity history mismatch.')
        if reason is not None and detail.get('IssueDifferenceReasonId') != reason:
            raise ValueError('Requirement history difference reason does not match the selected reason.')
        header = await one('ComponentIssueHistory',
            'SELECT TOP 2 ComponentIssueHistoryId,HistoryMainlineId,IntegrationTxnId,HistoryId '
            f"FROM ComponentIssueHistory WHERE ComponentIssueHistoryId='{header_id}'")
        # Header.TxnId is a service instance ID on this deployment, NOT main.TxnId.
        if (header.get('ComponentIssueHistoryId') != header_id or header.get('HistoryMainlineId') != main_id
                or header.get('IntegrationTxnId') != main_id or header.get('HistoryId') != target):
            raise ValueError('Component issue header is not linked to the same mainline/container.')
        actual = await one('IssueActualsHistory',
            'SELECT TOP 2 IssueActualsHistoryId,IssueHistoryDetailId,TxnId,HistoryId,ToContainerId,ProductId,'
            'Qty,Qty2,FromContainerId,FromLot,FromStockPoint '
            + (',IssueDifferenceReasonId ' if reason else '') +
            f"FROM IssueActualsHistory WHERE TxnId='{txn}'")
        actual_id = identifier(actual.get('IssueActualsHistoryId'))
        if (actual.get('IssueHistoryDetailId') != detail_id or actual.get('TxnId') != txn
                or actual.get('HistoryId') != target or actual.get('ToContainerId') != target
                or actual.get('ProductId') != product or number(actual.get('Qty')) != qty
                or number(actual.get('Qty2')) != 0
                or any(key not in actual or actual[key] not in ('', None)
                       for key in ('FromContainerId', 'FromLot', 'FromStockPoint'))):
            raise ValueError('Actual issue identity/quantity/source is not the planned quantity-only issue.')
        if reason is not None and actual.get('IssueDifferenceReasonId') != reason:
            raise ValueError('Actual history difference reason does not match the selected reason.')
        evidence.update(complete=True, verified_success=True, transaction_id=txn, mainline_id=main_id,
                        issue_detail_id=detail_id, actual_issue_id=actual_id, material_line_id=material,
                        verification_method='unique_marker_mainline_header_demand_line_actual_chain',
                        scope='this committed transaction only; later returns/consumption changes are not net balance')
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        evidence['error'] = str(error)
    return evidence
