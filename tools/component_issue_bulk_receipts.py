"""Read-only mode-2 receipt proof for the isolated, journaled contract probe."""
import hashlib
import json
import re

from config import CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import request
from tools.component_issue_bulk import build_preview, derived_request
from tools.component_issue_payload import reference
from tools.component_issue_receipts import identifier, number
from tools.modeling_details import raw_json


async def receipt(report, *, marker_prefix='Codex bulk '):
    out = {'complete': False, 'verified_success': False, 'reads': [],
           'mes_write_performed': False, 'original_probe_reusable': False,
           'scope': 'original mode-2 committed transaction; later source/balance changes are outside this proof'}
    try:
        body = report['body']; before = report['preview']; marker = report['marker']
        if (report.get('environment') != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/') or report.get('request_sent') is not True
                or not isinstance(marker, str) or not re.fullmatch(re.escape(marker_prefix)+'[0-9a-f]{32}', marker)
                or body.get('comments') != marker
                or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != report.get('body_digest')):
            raise ValueError('Probe environment/attempt/marker/digest does not match; no history query.')
        rebuilt, _ = build_preview(before['target_status']['data']['ContainerStatusDetails'],
            before['source_status']['data']['ContainerStatusDetails'], before['requirement'],
            before['source_balance'], before['current_children'], body['serviceDetails'][0]['actualQtyIssued'])
        if body != {**derived_request(rebuilt), 'comments': marker}:
            raise ValueError('Probe request is not the single-source derived mode-2 contract.')
        line = body['serviceDetails'][0]; actual = line['actuals'][0]
        target = identifier(body['container']['instanceId']); source = identifier(actual['fromContainer']['instanceId'])
        product = identifier(line['product']['instanceId']); unit = identifier(line['uom']['instanceId'])
        material = identifier(line['bomLineItem']['instanceId']); qty = number(actual['qtyIssued'])
        context = before['target_status']['data']['ContainerStatusDetails']
        spec = identifier(reference(context['Spec'], revisioned=True)['instanceId'])
        operation = identifier(reference(context['Operation'])['instanceId'])
        async def one(table, query):
            raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                body={'queryText': query}, response_formatter=raw_json, retry_read_only=True)
            out['reads'].append({'table': table, 'query': query, 'response': raw})
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError(table+': missing, duplicate or failed history is inconclusive.')
            return rows[0]
        main = await one('HistoryMainline', 'SELECT TOP 2 HistoryMainlineId,TxnId,ContainerId,TxnTypeName,SpecId,OperationId,Comments '
            f"FROM HistoryMainline WHERE ContainerId='{target}' AND Comments='{marker}'")
        main_id = identifier(main.get('HistoryMainlineId')); txn = identifier(main.get('TxnId'))
        if (main.get('ContainerId') != target or main.get('Comments') != marker or main.get('TxnTypeName') != 'ComponentIssue'
                or main.get('SpecId') != spec or main.get('OperationId') != operation):
            raise ValueError('Mainline target/type/marker/context does not match.')
        detail = await one('IssueHistoryDetail', 'SELECT TOP 2 IssueHistoryDetailId,ComponentIssueHistoryId,TxnId,IntegrationTxnId,HistoryId,'
            'ProductMaterialListItemId,ProductId,IssueControl,UOMId,QtyRequired,Qty2Required,ActualQtyIssued,ActualQty2Issued '
            f"FROM IssueHistoryDetail WHERE TxnId='{txn}'")
        detail_id = identifier(detail.get('IssueHistoryDetailId')); header_id = identifier(detail.get('ComponentIssueHistoryId'))
        if (detail.get('TxnId') != txn or detail.get('IntegrationTxnId') != main_id or detail.get('HistoryId') != target
                or detail.get('ProductMaterialListItemId') != material or detail.get('ProductId') != product
                or detail.get('UOMId') != unit or number(detail.get('IssueControl')) != 2
                or number(detail.get('QtyRequired')) != qty or number(detail.get('ActualQtyIssued')) != qty
                or number(detail.get('Qty2Required')) != 0 or number(detail.get('ActualQty2Issued')) != 0):
            raise ValueError('Mode-2 demand identity/quantity/transaction does not match.')
        header = await one('ComponentIssueHistory', 'SELECT TOP 2 ComponentIssueHistoryId,HistoryMainlineId,IntegrationTxnId,HistoryId '
            f"FROM ComponentIssueHistory WHERE ComponentIssueHistoryId='{header_id}'")
        if (header.get('ComponentIssueHistoryId') != header_id or header.get('HistoryMainlineId') != main_id
                or header.get('IntegrationTxnId') != main_id or header.get('HistoryId') != target):
            raise ValueError('Mode-2 history header is not linked to the same transaction.')
        row = await one('IssueActualsHistory', 'SELECT TOP 2 IssueActualsHistoryId,CDOTypeId,IssueHistoryDetailId,TxnId,HistoryId,'
            'ToContainerId,FromContainerId,FromLot,FromStockPoint,ProductId,Qty,Qty2 '
            f"FROM IssueActualsHistory WHERE TxnId='{txn}'")
        actual_id = identifier(row.get('IssueActualsHistoryId'))
        if (number(row.get('CDOTypeId')) != 6939 or row.get('IssueHistoryDetailId') != detail_id
                or row.get('TxnId') != txn or row.get('HistoryId') != target or row.get('ToContainerId') != target
                or row.get('FromContainerId') != source or row.get('ProductId') != product
                or number(row.get('Qty')) != qty or number(row.get('Qty2')) != 0
                or any(key not in row or row[key] not in ('', None) for key in ('FromLot', 'FromStockPoint'))):
            raise ValueError('Bulk history subtype/source/target/quantity does not match the original request.')
        out.update(complete=True, verified_success=True, transaction_id=txn, mainline_id=main_id,
                   issue_detail_id=detail_id, actual_issue_id=actual_id, source_container_id=source,
                   target_container_id=target, verification_method='unique_marker_mode2_bulk_source_history_chain')
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        out['error'] = str(error)
    return out


async def planned_receipt(payload):
    """Adapt a checked shared-library mode-2 snapshot, without changing state."""
    from tools.component_issue_plans import bulk_requirement
    snap = payload['snapshot']
    body = {**snap['body'], 'comments': payload['marker']}
    report = {'environment': snap['environment'], 'request_sent': True, 'body': body, 'marker': payload['marker'],
        'body_digest': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
        'preview': {'target_status': {'data': {'ContainerStatusDetails': snap['context']}},
                    'source_status': {'data': {'ContainerStatusDetails': snap['source_context']}},
                    'requirement': bulk_requirement(snap), 'source_balance': snap['source_balance'], 'current_children': []}}
    return await receipt(report, marker_prefix='Codex issue ')
