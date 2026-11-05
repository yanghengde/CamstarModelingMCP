"""Correlate approved loss, batch scrap and batch rework with persisted native history."""
import math
import re
from datetime import datetime, timezone
from decimal import Decimal

from tools import quality as q, quality_inspection as i


SERVICE_TYPES = {'quantity_loss': ('ChangeQty', 4000), 'lot_completion': ('ExecuteSamplingLot', 8980),
                 'lot_scrap': ('ExecuteSamplingLot', 8980),
                 'lot_rework': ('ExecuteSamplingLot', 8980)}
LOT_RESULTS = {'lot_completion': (1, None, None),
               'lot_scrap': (3, 'ScrapReasonId', 'ReworkReasonId'),
               'lot_rework': (2, 'ReworkReasonId', 'ScrapReasonId')}
MAINLINE = ('HistoryMainlineId,HistoryId,TxnId,TxnDateGMT,TxnType,TxnTypeName,IntegrationTxnId,ReversalStatus,Comments,'
            'ContainerId,ContainerName,ContainerChangeCount,ProductId,OperationId,SpecId,WorkflowStepId,Qty,Qty2,ToQty,ToQty2,InRework')
SUMMARY = 'QtyHistoryId,HistoryMainlineId,HistoryId,TxnId,IntegrationTxnId,CDOTypeId,Qty,Qty2,CloseWhenZero,ThruputSummary'
DETAIL = ('QtyHistoryDetailsId,QtyHistoryId,HistoryId,TxnId,IntegrationTxnId,CDOTypeId,ContainerId,ProductId,OperationId,'
          'ChargeToStepId,ReasonCodeId,ChangeQtyType,QtyMultiplier,Qty,Qty2,UOMId,UOM2Id,AdjustThruput,CountsAgainstProduction,'
          'RecordAllQty,CloseWhenZero,ThisContainerClosed,ThisContainerDisassociated,IsRollup')
LOT = ('ExecuteSamplingLotHistoryId,HistoryMainlineId,HistoryId,TxnId,IntegrationTxnId,CDOTypeId,'
       'SamplingLotResult,SamplingCompleted,ScrapReasonId,ReworkReasonId')
MOVE = ('MoveHistoryId,HistoryMainlineId,HistoryId,TxnId,CDOTypeId,ProductId,PathId,StepId,ToStepId,ToSpecId,'
        'ReEntryStepId,EndReworkStepId,ReworkReasonId,Qty,Qty2,CloseFlag,isFailed')
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


async def transaction(txn_id, kind):
    where = ' WHERE TxnId='+q.literal(identifier(txn_id))
    queries = {'mainlines': ('HistoryMainline', MAINLINE, 'HistoryMainlineId')}
    maximum = 52 if kind == 'lot_completion' else 22
    if kind != 'lot_completion':
        queries['summaries'] = ('QtyHistory', SUMMARY, 'QtyHistoryId')
        queries['details'] = ('QtyHistoryDetails', DETAIL, 'QtyHistoryDetailsId')
    if kind in LOT_RESULTS:
        queries['lot'] = ('ExecuteSamplingLotHistory', LOT, 'ExecuteSamplingLotHistoryId')
    if kind == 'lot_rework':
        queries['moves'] = ('MoveHistory', MOVE, 'MoveHistoryId')
    evidence = {}
    for label, (table, fields, key) in queries.items():
        rows = await q.sql_rows(f'SELECT TOP {maximum} '+fields+' FROM '+table+where+' ORDER BY '+key, maximum)
        ids = [identifier(row[key]) for row in rows]
        if len(ids) >= maximum or len(ids) != len(set(ids)) or any(row['TxnId'] != txn_id for row in rows):
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


def rework_matches(record, route, mainline, move, root_id, reason):
    status = route['status']
    source = identifier(route['source_step']['instanceId'])
    end = route['end_rework']
    end_id = identifier(q.identifier(end)) if end else None
    qty = quantity(record['Qty'])
    return (
        mainline['ContainerId'] == mainline['HistoryId'] == move['HistoryId'] == route['container']['instanceId']
        and mainline['ContainerName'] == record['ContainerName'] == route['container']['name']
        and mainline['HistoryMainlineId'] == move['HistoryMainlineId']
        and mainline['IntegrationTxnId'] == root_id
        and mainline['ProductId'] == move['ProductId'] == identifier(record['ProductId'])
        and mainline['SpecId'] == identifier(q.identifier(status['Spec']))
        and mainline['OperationId'] == identifier(q.identifier(status['Operation']))
        and mainline['WorkflowStepId'] == move['StepId'] == source == q.identifier(status['Step'])
        and i.wire_int(mainline['ContainerChangeCount'], '返工前容器版本') == i.wire_int(record['ChangeCount'], '原容器版本')
        and i.wire_int(mainline['TxnType'], '返工事务类型') == 6660 and mainline['TxnTypeName'] == 'Rework'
        and i.wire_int(mainline['ReversalStatus'], '返工历史冲销状态') == 1
        and i.wire_int(move['CDOTypeId'], '移动历史类型') == 6700
        and flags_match(mainline, InRework=True) and flags_match(move, CloseFlag=False, isFailed=False)
        and move['PathId'] == identifier(route['path']['instanceId'])
        and move['ToStepId'] == identifier(route['to_step']['instanceId'])
        and move['ToSpecId'] == identifier(route['target_spec']['instanceId'])
        and move['ReEntryStepId'] == identifier(route['reentry_step']['instanceId'])
        and (move['EndReworkStepId'] or None) == end_id
        and move['ReworkReasonId'] == reason
        and qty > 0 and quantity(mainline['Qty']) == quantity(mainline['ToQty']) == quantity(move['Qty']) == qty
        and all(quantity(value, nullable_zero=True) == 0
                for value in (record.get('Qty2'), mainline['Qty2'], mainline['ToQty2'], move['Qty2']))
    )


def compare_rework(snap, evidence, mainlines, root_id, reason):
    records = snap['before']['members']
    routes = snap['preview']['member_routes']
    members_by_id = {identifier(record['ContainerId']): record for record in records}
    routes_by_id = {identifier(route['container']['instanceId']): route for route in routes}
    if not (1 <= len(records) == len(members_by_id) == len(routes) == len(routes_by_id) <= 20
            and members_by_id.keys() == routes_by_id.keys()):
        raise ValueError('原返工计划成员或路径不完整。')
    moves = {row['HistoryMainlineId']: row for row in evidence['moves']}
    exact = (len(mainlines) == len(evidence['moves']) == len(moves) == len(members_by_id)
             and {row['ContainerId'] for row in mainlines} == set(members_by_id)
             and {row['HistoryMainlineId'] for row in mainlines} == set(moves))
    return {
        'exact_members_and_move_counts': exact,
        'no_quantity_adjustment_history': not (evidence['summaries'] or evidence['details']),
        'member_rework_history': exact and all(
            rework_matches(members_by_id[row['ContainerId']], routes_by_id[row['ContainerId']], row,
                           moves[row['HistoryMainlineId']], root_id, reason)
            for row in mainlines
        ),
    }


def compare(snap, attempt, root, evidence):
    kind = snap['kind']
    service, service_type = SERVICE_TYPES[kind]
    root_id = root['HistoryMainlineId']
    checks = {
        'root_in_transaction': root in evidence['mainlines'],
        'attempt_time_window': attempt_time_matches(attempt,root),
        'active_native_service': (root['TxnTypeName'] == service and i.wire_int(root['TxnType'],'父事务类型') == service_type
                                  and i.wire_int(root['ReversalStatus'],'父历史冲销状态') == 1
                                  and root['IntegrationTxnId'] == root_id),
    }
    mainlines = evidence['mainlines']
    if kind in LOT_RESULTS:
        result, reason_field, unused_reason_field = LOT_RESULTS[kind]
        batch = evidence['lot']
        checks['lot_identity_and_result'] = (root['ContainerId'] in ('',None) and len(batch) == 1
            and batch[0]['HistoryMainlineId'] == batch[0]['IntegrationTxnId'] == root_id
            and batch[0]['HistoryId'] == snap['target_id']
            and i.wire_int(batch[0]['CDOTypeId'],'批历史类型') == 8989
            and i.wire_int(batch[0]['SamplingLotResult'],'抽样批处置结果') == result
            and i.wire_bool(batch[0]['SamplingCompleted'],'抽样批完成'))
        if kind == 'lot_completion':
            checks['fixed_request_target'] = (snap['body']['samplingLot']['instanceId'] == snap['target_id'])
            checks['no_disposition_reason'] = len(batch) == 1 and all(
                batch[0][field] in ('', None) for field in ('ScrapReasonId', 'ReworkReasonId'))
            return checks
        reason = identifier(snap['reason']['instanceId'])
        checks['lot_identity_and_result'] = (checks['lot_identity_and_result']
            and batch[0][reason_field] == reason and batch[0][unused_reason_field] in ('',None))
        mainlines = [row for row in mainlines if row['HistoryMainlineId'] != root_id]
    else:
        reason = identifier(snap['reason']['instanceId'])
    if kind == 'lot_rework':
        return checks | compare_rework(snap, evidence, mainlines, root_id, reason)
    plan_members = members(snap)
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
    kind = snap['kind']
    evidence = await transaction(root['TxnId'], kind)
    checks = compare(snap, attempt, root, evidence)
    if await roots(snap, limit=1) != (first,False) or await transaction(root['TxnId'],kind) != evidence:
        raise ValueError('核对期间原标记或原生处置事务历史发生变化。')
    matched = all(checks.values())
    if kind == 'lot_completion':
        return {'comparison': 'native_request_match' if matched else 'mismatch', 'complete': True,
                'candidate_txn_id': root['TxnId'], 'checks': checks, 'evidence': evidence,
                'request_correlation_verified': matched, 'recorded_business_effects_verified': False,
                'unverified_checks': ['整批OK子事务和成员最终影响仍需非空现场验收。']}
    return {'comparison': 'native_history_match' if matched else 'mismatch', 'complete': True,
            'candidate_txn_id': root['TxnId'], 'checks': checks, 'evidence': evidence,
            'request_correlation_verified': matched, 'recorded_business_effects_verified': matched,
            'unverified_checks': ['当前容器状态可能已被后续事务改变，须从原ID单独核查。']
                + (['采样任务清理须从原成员和任务基线单独核查。'] if kind in LOT_RESULTS else [])}
