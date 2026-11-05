"""Correlate approved loss and batch scrap requests with persisted native history."""
import math
import re
from datetime import datetime, timezone
from decimal import Decimal

from tools import quality as q, quality_inspection as i


SERVICE_TYPES = {'quantity_loss': ('ChangeQty', 4000), 'lot_scrap': ('ExecuteSamplingLot', 8980)}
MAINLINE = ('HistoryMainlineId,HistoryId,TxnId,TxnDateGMT,TxnType,TxnTypeName,IntegrationTxnId,ReversalStatus,Comments,'
            'ContainerId,ContainerName,ContainerChangeCount,ProductId,OperationId,SpecId,WorkflowStepId,Qty,Qty2,ToQty,ToQty2')
SUMMARY = 'QtyHistoryId,HistoryMainlineId,HistoryId,TxnId,IntegrationTxnId,CDOTypeId,Qty,Qty2,CloseWhenZero,ThruputSummary'
DETAIL = ('QtyHistoryDetailsId,QtyHistoryId,HistoryId,TxnId,IntegrationTxnId,CDOTypeId,ContainerId,ProductId,OperationId,'
          'ChargeToStepId,ReasonCodeId,ChangeQtyType,QtyMultiplier,Qty,Qty2,UOMId,UOM2Id,AdjustThruput,CountsAgainstProduction,'
          'RecordAllQty,CloseWhenZero,ThisContainerClosed,ThisContainerDisassociated,IsRollup')
LOT = ('ExecuteSamplingLotHistoryId,HistoryMainlineId,HistoryId,TxnId,IntegrationTxnId,CDOTypeId,'
       'SamplingLotResult,SamplingCompleted,ScrapReasonId,ReworkReasonId')
CLOCK_TOLERANCE_SECONDS = 5


def identifier(value):
    if not q.valid_id(value):
        raise ValueError('处置历史缺少明确的持久ID。')
    return value


def quantity(value, *, nullable_zero=False):
    if nullable_zero and value in ('', None):
        return Decimal(0)
    return i.numeric(str(value), '历史数量')


def flags_match(row, **expected):
    return all(i.wire_bool(row[field], field) is value for field,value in expected.items())


def comments(snap):
    marker = snap['marker']
    value = snap['body']['comments']
    if (not isinstance(marker, str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}', marker)
            or not (value == marker or value.startswith(marker+' '))):
        raise ValueError('处置计划缺少系统生成的完整事务标记。')
    return value


async def roots(snap, offset=0, limit=20):
    q.paging(offset, limit)
    value = comments(snap)
    rows = await q.sql_rows('SELECT '+MAINLINE+' FROM HistoryMainline WHERE Comments='+q.literal(value)
        +f' ORDER BY HistoryMainlineId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY', limit+1)
    for row in rows:
        identifier(row['HistoryMainlineId'])
        identifier(row['TxnId'])
        if row['Comments'] != value:
            raise ValueError('候选主历史未完整匹配原评论。')
    return rows[:limit], len(rows) > limit


async def transaction(txn_id, scrap):
    where = ' WHERE TxnId='+q.literal(identifier(txn_id))
    queries = {'mainlines': ('HistoryMainline', MAINLINE, 'HistoryMainlineId'),
               'summaries': ('QtyHistory', SUMMARY, 'QtyHistoryId'),
               'details': ('QtyHistoryDetails', DETAIL, 'QtyHistoryDetailsId')}
    if scrap:
        queries['lot'] = ('ExecuteSamplingLotHistory', LOT, 'ExecuteSamplingLotHistoryId')
    evidence = {}
    for label, (table, fields, key) in queries.items():
        rows = await q.sql_rows('SELECT TOP 22 '+fields+' FROM '+table+where+' ORDER BY '+key, 22)
        ids = [identifier(row[key]) for row in rows]
        if len(ids) >= 22 or len(ids) != len(set(ids)) or any(row['TxnId'] != txn_id for row in rows):
            raise ValueError('处置事务历史超界、重复或事务组不符。')
        evidence[label] = rows
    return evidence


def attempt_time_matches(attempt, row):
    started, finished = attempt['started'], attempt['finished']
    if (not isinstance(attempt.get('attempt_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', attempt['attempt_id'])
            or any(type(value) not in (int,float) or not math.isfinite(value) for value in (started,finished))
            or finished < started):
        return False
    value = row.get('TxnDateGMT')
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|\+00:00)?',value):
        return False
    date = datetime.fromisoformat(value)
    return started-CLOCK_TOLERANCE_SECONDS <= date.replace(tzinfo=timezone.utc).timestamp() <= finished+CLOCK_TOLERANCE_SECONDS


def members(snap):
    if snap['kind'] == 'lot_scrap':
        planned = snap['preview']['member_dispositions']
        source_ids = [identifier(row['ContainerId']) for row in snap['before']['members']]
        ids = [identifier(member['container']['instanceId']) for member in planned]
        if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids) or sorted(ids) != sorted(source_ids):
            raise ValueError('原报废计划成员身份或范围不完整。')
        return {key: {
            'record': member['record'], 'status': member['status'],
            'loss': member['expected_loss_quantity'], 'remaining': member['expected_remaining_quantity'],
            'loss2': '0', 'remaining2': '0', 'all': True, 'closed': True,
            'close_when_zero': member['native_defaults']['root']['CloseWhenZero'],
            'adjust_throughput': member['native_defaults']['adjust_throughput'],
        } for key,member in zip(ids,planned)}
    line = snap['body']['serviceDetails'][0]
    preview = snap['preview']
    return {identifier(snap['target_id']): {
        'record': snap['before']['container'], 'status': snap['before']['container_status'],
        'loss': snap['expected_loss_quantity'], 'remaining': preview['projected_remaining_quantity'],
        'loss2': preview.get('secondary_loss_quantity','0'), 'remaining2': preview.get('projected_remaining_secondary_quantity','0'),
        'all': line['recordAllQty'], 'closed': preview['projected_close'], 'close_when_zero': snap['body']['closeWhenZero'],
        'adjust_throughput': line['adjustThruput'],
    }}


def loss_matches(member, mainline, summary, detail, root, reason):
    record = member['record']
    cid = identifier(record['ContainerId'])
    op = identifier(q.identifier(member['status']['Operation']))
    unit2 = record['UOM2Id'] or None
    loss, loss2 = quantity(member['loss']), quantity(member['loss2'])
    return (
        mainline['ContainerId'] == mainline['HistoryId'] == summary['HistoryId'] == detail['HistoryId'] == detail['ContainerId'] == cid
        and mainline['ContainerName'] == record['ContainerName']
        and mainline['HistoryMainlineId'] == summary['HistoryMainlineId']
        and mainline['IntegrationTxnId'] == summary['IntegrationTxnId'] == detail['IntegrationTxnId'] == root['HistoryMainlineId']
        and summary['QtyHistoryId'] == detail['QtyHistoryId']
        and mainline['ProductId'] == detail['ProductId'] == record['ProductId']
        and mainline['SpecId'] == record['SpecId']
        and mainline['OperationId'] == detail['OperationId'] == op
        and mainline['WorkflowStepId'] == detail['ChargeToStepId'] == record['WorkflowStepId']
        and i.wire_int(mainline['ContainerChangeCount'],'历史容器版本') == record['ChangeCount']
        and i.wire_int(mainline['TxnType'],'数量事务类型') == 4000 and mainline['TxnTypeName'] == 'ChangeQty'
        and i.wire_int(mainline['ReversalStatus'],'数量历史冲销状态') == 1
        and i.wire_int(summary['CDOTypeId'],'数量汇总类型') == 5080
        and i.wire_int(detail['CDOTypeId'],'损失历史类型') == 6932
        and i.wire_int(detail['ChangeQtyType'],'数量处置类型') == 2 and str(detail['QtyMultiplier']) == '-1'
        and detail['ReasonCodeId'] == reason
        and detail['UOMId'] == record['UOMId'] and (detail['UOM2Id'] or None) == unit2
        and quantity(mainline['Qty']) == quantity(record['Qty'])
        and quantity(mainline['Qty2'],nullable_zero=unit2 is None) == quantity(record['Qty2'],nullable_zero=unit2 is None)
        and quantity(mainline['ToQty']) == quantity(member['remaining'])
        and quantity(mainline['ToQty2'],nullable_zero=unit2 is None) == quantity(member['remaining2'])
        and quantity(summary['Qty']) == -loss and quantity(summary['Qty2'],nullable_zero=unit2 is None) == -loss2
        and quantity(detail['Qty']) == loss and quantity(detail['Qty2'],nullable_zero=unit2 is None) == loss2
        and flags_match(summary, CloseWhenZero=member['close_when_zero'], ThruputSummary=False)
        and flags_match(detail, AdjustThruput=member['adjust_throughput'], CountsAgainstProduction=True,
                        RecordAllQty=member['all'], CloseWhenZero=member['close_when_zero'],
                        ThisContainerClosed=member['closed'], ThisContainerDisassociated=False, IsRollup=False)
    )


def compare(snap, attempt, root, evidence):
    kind = snap['kind']
    scrap = kind == 'lot_scrap'
    service, service_type = SERVICE_TYPES[kind]
    plan_members = members(snap)
    root_id = root['HistoryMainlineId']
    reason = identifier(snap['reason']['instanceId'])
    checks = {
        'root_in_transaction': root in evidence['mainlines'],
        'attempt_time_window': attempt_time_matches(attempt,root),
        'active_native_service': (root['TxnTypeName'] == service and i.wire_int(root['TxnType'],'父事务类型') == service_type
                                  and i.wire_int(root['ReversalStatus'],'父历史冲销状态') == 1
                                  and root['IntegrationTxnId'] == root_id),
    }
    if scrap:
        batch = evidence['lot']
        checks['lot_identity_and_result'] = (root['ContainerId'] in ('',None) and len(batch) == 1
            and batch[0]['HistoryMainlineId'] == batch[0]['IntegrationTxnId'] == root_id
            and batch[0]['HistoryId'] == snap['target_id'] and batch[0]['ScrapReasonId'] == reason
            and batch[0]['ReworkReasonId'] in ('',None)
            and i.wire_int(batch[0]['CDOTypeId'],'批历史类型') == 8989
            and i.wire_int(batch[0]['SamplingLotResult'],'抽样批处置结果') == 3
            and i.wire_bool(batch[0]['SamplingCompleted'],'抽样批完成'))
    mainlines = [row for row in evidence['mainlines'] if not scrap or row['HistoryMainlineId'] != root_id]
    checks['exact_members_and_detail_counts'] = (
        len(mainlines) == len(evidence['summaries']) == len(evidence['details']) == len(plan_members)
        and {row['ContainerId'] for row in mainlines} == set(plan_members)
    )
    if checks['exact_members_and_detail_counts']:
        summaries = {row['HistoryMainlineId']: row for row in evidence['summaries']}
        details = {row['QtyHistoryId']: row for row in evidence['details']}
        checks['member_loss_history'] = all(
            (summary := summaries.get(row['HistoryMainlineId'])) is not None
            and (detail := details.get(summary['QtyHistoryId'])) is not None
            and loss_matches(plan_members[row['ContainerId']], row, summary, detail, root, reason)
            for row in mainlines
        )
    return checks


async def verify(snap, attempt, candidate_txn_id=None):
    first, more = await roots(snap, limit=1)
    if more or not first:
        return {'comparison': 'ambiguous' if more else 'no_records', 'complete': False,
                'candidates': first, 'request_correlation_verified': False}
    root = first[0]
    if candidate_txn_id is not None and root['TxnId'] != candidate_txn_id:
        return {'comparison': 'candidate_mismatch', 'complete': False, 'candidates': first,
                'request_correlation_verified': False}
    scrap = snap['kind'] == 'lot_scrap'
    evidence = await transaction(root['TxnId'], scrap)
    checks = compare(snap, attempt, root, evidence)
    if await roots(snap, limit=1) != (first,False) or await transaction(root['TxnId'],scrap) != evidence:
        raise ValueError('核对期间原标记或原生处置事务历史发生变化。')
    matched = all(checks.values())
    return {'comparison': 'native_history_match' if matched else 'mismatch', 'complete': True,
            'candidate_txn_id': root['TxnId'], 'checks': checks, 'evidence': evidence,
            'request_correlation_verified': matched, 'recorded_business_effects_verified': matched,
            'unverified_checks': ['当前容器状态可能已被后续事务改变，须从原ID单独核查。']
                + (['采样任务清理不在损失历史字段内，须从原成员和任务基线单独核查。'] if scrap else [])}
