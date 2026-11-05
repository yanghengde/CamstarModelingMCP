"""Conservative local audit criteria for transactions with proven native effects."""
from tools import quality_disposition_history as history
from tools import quality_inspection as inspection
from tools import quality_plans as plans


SUPPORTED = frozenset({'quantity_loss', 'lot_scrap', 'lot_rework'})


def balance_matches(current, member):
    if current is None:
        return False
    before = member['record']
    no_secondary_unit = not before['UOM2Id']
    return (
        all(current[field] == before[field] for field in
            ('ContainerId', 'ContainerName', 'ProductId', 'UOMId', 'UOM2Id', 'SpecId', 'WorkflowStepId'))
        and current['Status'] == (2 if member['closed'] else 1)
        and current['ChangeCount'] > before['ChangeCount']
        and history.quantity(current['Qty']) == history.quantity(member['remaining'])
        and history.quantity(current['Qty2'], nullable_zero=no_secondary_unit) == history.quantity(member['remaining2'])
    )


async def current_evidence(snap):
    kind = snap['kind']
    if kind == 'quantity_loss':
        from tools.quality_quantity_loss import record
        member = history.members(snap)[snap['target_id']]
        current = await record(snap['target_id'])
        return {'original_balance_and_status': balance_matches(current, member)}, current
    if kind == 'lot_scrap':
        from tools.quality_lot_scrap import inspect_snapshot
        evidence = await inspect_snapshot(snap)
        members = history.members(snap)
        current = {row['container_id']: row['current_record'] for row in evidence['original_members']}
        checks = {'original_balances_and_statuses': current.keys() == members.keys()
                  and all(balance_matches(current[cid], member) for cid, member in members.items())}
    else:
        from tools.quality_lot_rework import inspect_snapshot
        evidence = await inspect_snapshot(snap)
        members = {row['ContainerId']: row for row in snap['before']['members']}
        current = {row['ContainerId']: row for row in evidence['original_members_current_records']}
        checks = {
            'original_member_routes': not evidence['missing_original_member_ids']
                and {row['container_id'] for row in evidence['member_route_evidence']} == members.keys()
                and all(row['available'] and row['expected_route_currently_observed'] and row['quantity_currently_unchanged']
                        for row in evidence['member_route_evidence']),
            'original_member_statuses': current.keys() == members.keys() and all(
                row['ProductId'] == members[cid]['ProductId']
                and inspection.wire_int(row['Status'], '当前返工成员状态') == 1
                and inspection.wire_int(row['ChangeCount'], '当前容器版本')
                    > inspection.wire_int(members[cid]['ChangeCount'], '原容器版本')
                and history.quantity(row['Qty'], nullable_zero=False) == history.quantity(members[cid]['Qty'])
                and history.quantity(row['Qty2'], nullable_zero=True) == 0 for cid, row in current.items()),
        }
    lot = evidence['current_lot']['lot']
    cleanup = evidence['sampling_task_cleanup']
    checks['original_lot_result'] = (lot['MfgLotId'] == snap['target_id'] and lot['SamplingCompleted'] is True
                                     and lot['SamplingLotResult'] == history.LOT_RESULTS[kind][0])
    checks['original_tasks_removed'] = (cleanup['baseline_available'] and cleanup['all_original_tasks_absent']
                                        and not cleanup['new_tasks'])
    return checks, evidence


async def verified_evidence(snap, attempt, txn_id):
    if snap['kind'] not in SUPPORTED:
        raise ValueError('该事务的最终效果须以人工服务端审计证据结案。')
    proof = await history.verify(snap, attempt, txn_id)
    if not (proof.get('request_correlation_verified') and proof.get('recorded_business_effects_verified')):
        raise ValueError('指定事务未通过原生历史核对，继续保留待审占用。')
    checks, current = await current_evidence(snap)
    if not all(checks.values()):
        raise ValueError('当前原对象的状态、数量、路线或任务清理未全部匹配，继续保留待审占用。')
    if await current_evidence(snap) != (checks, current) or await history.verify(snap, attempt, txn_id) != proof:
        raise ValueError('审计期间历史或当前证据发生变化，继续保留待审占用。')
    return {'candidate_txn_id': txn_id, 'attempt': attempt, 'history_checks': proof['checks'],
            'current_checks': checks, 'evidence_digest': plans.digest({'history': proof['evidence'], 'current': current})}
