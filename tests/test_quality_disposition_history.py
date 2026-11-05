import asyncio
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tools import quality_disposition_history as disposition, quality_history as history, quality_plans as plans


def key(number):
    return f'{number:016x}'


CONTAINER, PRODUCT, SPEC, STEP, OPERATION, UNIT, UNIT2, REASON, ROOT, TXN, SUMMARY, DETAIL = map(key, range(1,13))
MARKER = 'MCP-QUAL:'+'f'*32
TIME = '2026-11-04T15:01:38.127'
EPOCH = datetime.fromisoformat(TIME).replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(plans,'PLAN_DB',tmp_path/'quality.sqlite3')
    reason = {'name': 'SCRAP', 'instanceId': REASON}
    record = {'ContainerId': CONTAINER, 'ContainerName': 'C', 'ProductId': PRODUCT, 'SpecId': SPEC,
              'WorkflowStepId': STEP, 'UOMId': UNIT, 'UOM2Id': UNIT2, 'Qty': '10', 'Qty2': '5', 'ChangeCount': 1}
    snap = {'kind': 'quantity_loss', 'action': 'loss', 'service': 'ChangeQty', 'target_id': CONTAINER, 'name': 'C',
            'environment': plans.environment(), 'marker': MARKER, 'reason': reason, 'expected_loss_quantity': '2',
            'before': {'container': record,'container_status': {'Operation': {'InstanceId': OPERATION}}},
            'arguments': {'close_when_zero': True, 'record_all_quantity': False, 'adjust_throughput': True,
                          'loss_quantity': '2', 'secondary_loss_quantity': '1'},
            'preview': {'projected_remaining_quantity': '8', 'secondary_loss_quantity': '1',
                        'projected_remaining_secondary_quantity': '4', 'projected_close': False},
            'body': {'container': {'name': 'C', 'instanceId': CONTAINER}, 'comments': MARKER+' reason',
                     'closeWhenZero': True, 'disassociateWhenClosed': False, 'generateRollups': False,
                     'recordAllQtySummaryOnly': False, 'serviceDetails': [{'@odata.type': '#shopfloor.LossDetails',
                     'changeQtyType': 2, 'recordAllQty': False, 'adjustThruput': True,
                     'qty': 2.0, 'qty2': 1.0, 'reasonCode': reason}]}}
    root = {'HistoryMainlineId': ROOT, 'HistoryId': CONTAINER, 'TxnId': TXN, 'IntegrationTxnId': ROOT,
            'TxnDateGMT': TIME, 'TxnType': '4000', 'TxnTypeName': 'ChangeQty', 'Comments': snap['body']['comments'],
            'ContainerId': CONTAINER, 'ContainerName': 'C', 'ContainerChangeCount': '1', 'ReversalStatus': '1',
            'ProductId': PRODUCT, 'OperationId': OPERATION, 'SpecId': SPEC, 'WorkflowStepId': STEP,
            'Qty': '10', 'Qty2': '5', 'ToQty': '8', 'ToQty2': '4'}
    summary = {'QtyHistoryId': SUMMARY, 'HistoryMainlineId': ROOT, 'HistoryId': CONTAINER, 'TxnId': TXN,
               'IntegrationTxnId': ROOT, 'CDOTypeId': '5080', 'Qty': '-2', 'Qty2': '-1',
               'CloseWhenZero': 'true', 'ThruputSummary': 'false'}
    detail = {'QtyHistoryDetailsId': DETAIL, 'QtyHistoryId': SUMMARY, 'HistoryId': CONTAINER, 'TxnId': TXN,
              'IntegrationTxnId': ROOT, 'CDOTypeId': '6932', 'ContainerId': CONTAINER, 'ProductId': PRODUCT,
              'OperationId': OPERATION, 'ChargeToStepId': STEP, 'ReasonCodeId': REASON, 'ChangeQtyType': '2',
              'QtyMultiplier': '-1', 'Qty': '2', 'Qty2': '1', 'UOMId': UNIT, 'UOM2Id': UNIT2,
              'AdjustThruput': 'true', 'CountsAgainstProduction': 'true', 'RecordAllQty': 'false',
              'CloseWhenZero': 'true', 'ThisContainerClosed': 'false', 'ThisContainerDisassociated': 'false',
              'IsRollup': 'false'}
    state = {'snapshot': snap, 'roots': [root], 'mainlines': [root], 'summaries': [summary], 'details': [detail],
             'lot': [], 'moves': [], 'queries': []}

    async def sql(query, maximum):
        state['queries'].append(query)
        assert query.startswith('SELECT ')
        if 'WHERE Comments=' in query:
            assert disposition.q.literal(state['snapshot']['body']['comments']) in query
            return copy.deepcopy(state['roots'][:maximum])
        assert "WHERE TxnId=N'"+TXN+"'" in query
        table = {'FROM HistoryMainline ': 'mainlines', 'FROM QtyHistory ': 'summaries',
                 'FROM QtyHistoryDetails ': 'details', 'FROM ExecuteSamplingLotHistory ': 'lot',
                 'FROM MoveHistory ': 'moves'}
        for token, label in table.items():
            if token in query:
                rows = copy.deepcopy(state[label])
                if state.get('changed') == label and sum(token in q and 'WHERE TxnId=' in q for q in state['queries']) > 1:
                    rows[0]['ChangeCount'] = '2'
                return rows
        raise AssertionError(query)

    monkeypatch.setattr(disposition.q,'sql_rows',sql)
    return state


def install(env, state='unknown', started=EPOCH-1, finished=EPOCH+1, attempted=True):
    snap = env['snapshot']
    digest = plans.digest(snap)
    with plans.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)', ('plan',digest,json.dumps(snap),state,started-10))
        if attempted:
            db.execute('INSERT INTO attempts VALUES (?,?,?,?)', ('plan','a'*32,started,finished))
    return digest


def reconcile(digest, candidate=None):
    return json.loads(asyncio.run(history.reconcile_quality_execution('plan',digest,candidate)))


def test_exact_dual_loss_history_attributed_without_unlocking(env):
    digest = install(env)
    result = reconcile(digest)
    assert result['comparison'] == 'native_history_match'
    assert result['request_correlation_verified'] and result['recorded_business_effects_verified']
    assert result['candidate_txn_id'] == TXN
    assert not result['verified_success'] and not result['state_changed'] and not result['retry_allowed']
    with plans.db_connect() as db:
        assert db.execute('SELECT state,outcome FROM plans').fetchone() == ('unknown',None)
        assert not db.execute('SELECT 1 FROM reviews').fetchall()


@pytest.mark.parametrize(('table','field','bad'), [
    ('roots','ReversalStatus','2'), ('roots','ContainerChangeCount','2'), ('roots','ToQty2','3'),
    ('summaries','Qty2','0'), ('summaries','HistoryMainlineId',key(99)), ('summaries','CloseWhenZero','false'),
    ('details','IntegrationTxnId',key(99)), ('details','ReasonCodeId',key(99)), ('details','UOM2Id',UNIT),
    ('details','AdjustThruput','false'), ('details','ThisContainerClosed','true'), ('details','IsRollup','true'),
])
def test_same_marker_does_not_verify_wrong_history(env, table, field, bad):
    env[table][0][field] = bad
    result = reconcile(install(env))
    assert result['comparison'] == 'mismatch' and not result['request_correlation_verified']


@pytest.mark.parametrize('case', ['absent', 'duplicate', 'wrong_candidate', 'extra_detail', 'readback_changed', 'wrong_time', 'unfinished'])
def test_ambiguous_or_unstable_history_is_never_attributed(env, case):
    candidate = None
    started, finished = EPOCH-1, EPOCH+1
    if case == 'absent':
        env['roots'] = []
    elif case == 'duplicate':
        env['roots'].append(env['roots'][0] | {'HistoryMainlineId': key(99)})
    elif case == 'wrong_candidate':
        candidate = key(99)
    elif case == 'extra_detail':
        env['details'].append(env['details'][0] | {'QtyHistoryDetailsId': key(99)})
    elif case == 'readback_changed':
        env['changed'] = 'details'
    elif case == 'wrong_time':
        started, finished = EPOCH+20, EPOCH+21
    else:
        finished = None
    assert not reconcile(install(env,started=started,finished=finished),candidate)['request_correlation_verified']


@pytest.mark.parametrize(('state','attempted'), [('ready',False), ('unknown',False), ('human_committed',True)])
def test_no_history_read_without_eligible_local_attempt(env,state,attempted):
    assert 'error' in reconcile(install(env,state=state,attempted=attempted))
    assert not env['queries']


def test_candidate_listing_and_wrong_fingerprint(env):
    digest = install(env)
    assert 'error' in reconcile('0'*64)
    assert not env['queries']
    result = json.loads(asyncio.run(history.find_quality_execution_candidates('plan',digest)))
    assert result['rows'][0]['TxnId'] == TXN and result['page_complete']
    assert not result['request_correlation_verified']


@pytest.mark.parametrize('case', ['match', 'wrong_lot', 'missing_member', 'wrong_result'])
def test_batch_scrap_requires_parent_lot_and_exact_members(env, case):
    lot = key(30)
    snap = env['snapshot']
    original = snap['before']['container']
    root = env['roots'][0] | {'ContainerId': '', 'HistoryId': '', 'TxnTypeName': 'ExecuteSamplingLot', 'TxnType': '8980'}
    env['roots'] = [root]
    before, dispositions = [], []
    for n in (40,50):
        cid, mid, sid, did = map(key,range(n,n+4))
        record = original | {'ContainerId': cid, 'ContainerName': f'C{n}', 'Qty': '1', 'Qty2': '', 'UOM2Id': ''}
        before.append(record)
        dispositions.append({'container': {'name': f'C{n}', 'instanceId': cid}, 'record': record,
            'status': snap['before']['container_status'], 'expected_loss_quantity': '1', 'expected_remaining_quantity': '0',
            'native_defaults': {'root': {'CloseWhenZero': True}, 'adjust_throughput': True}})
        env['mainlines'].append(env['mainlines'][0] | {'HistoryMainlineId': mid,'ContainerId': cid,'HistoryId': cid,
            'ContainerName': f'C{n}', 'Qty': '1','Qty2': '', 'ToQty': '0', 'ToQty2': '0', 'Comments': ''})
        env['summaries'].append(env['summaries'][0] | {'HistoryMainlineId': mid,'QtyHistoryId': sid,'HistoryId': cid,
            'Qty': '-1','Qty2': '0'})
        env['details'].append(env['details'][0] | {'QtyHistoryId': sid,'QtyHistoryDetailsId': did,'ContainerId': cid,
            'HistoryId': cid,'Qty': '1','Qty2': '', 'UOM2Id': '', 'ThisContainerClosed': 'true', 'RecordAllQty': 'true'})
    env['mainlines'][0] = root
    env['summaries'].pop(0)
    env['details'].pop(0)
    env['lot'] = [{'ExecuteSamplingLotHistoryId': key(31), 'HistoryMainlineId': ROOT, 'HistoryId': lot, 'TxnId': TXN,
        'IntegrationTxnId': ROOT, 'CDOTypeId': '8989', 'SamplingLotResult': '3', 'SamplingCompleted': 'true',
        'ScrapReasonId': REASON, 'ReworkReasonId': ''}]
    snap.update(kind='lot_scrap', action='scrap', service='ExecuteSamplingLot', name='L', target_id=lot,
                before={'members': before}, body={'samplingLot': {'name': 'L', 'instanceId': lot}, 'samplingLotResult': 3,
                'scrapReason': snap['reason'], 'comments': root['Comments']},
                preview={'member_dispositions': dispositions, 'reason_binding':
                         {'concrete_type': 'LossReason', 'instance_id': REASON, 'load_verified': True}})
    if case == 'wrong_lot':
        env['lot'][0]['HistoryId'] = key(999)
    elif case == 'missing_member':
        env['mainlines'].pop()
    elif case == 'wrong_result':
        env['lot'][0]['SamplingLotResult'] = '2'
    result = reconcile(install(env))
    assert result['request_correlation_verified'] is (case == 'match')
    assert result['comparison'] == ('native_history_match' if case == 'match' else 'mismatch')


@pytest.fixture
def rework(env):
    lot = key(30)
    snap = env['snapshot']
    template = env['roots'][0]
    root = template | {'ContainerId': '', 'HistoryId': '', 'TxnTypeName': 'ExecuteSamplingLot', 'TxnType': '8980'}
    env.update(roots=[root], mainlines=[root], summaries=[], details=[])
    env['lot'] = [{'ExecuteSamplingLotHistoryId': key(31), 'HistoryMainlineId': ROOT, 'HistoryId': lot, 'TxnId': TXN,
                   'IntegrationTxnId': ROOT, 'CDOTypeId': '8989', 'SamplingLotResult': '2', 'SamplingCompleted': 'true',
                   'ScrapReasonId': '', 'ReworkReasonId': REASON}]
    members, routes = [], []
    for n, qty, end in [(40, '1', STEP), (50, '3.5', '')]:
        cid, mid, move_id, path, target, target_spec, entry = map(key,range(n,n+7))
        name = f'C{n}'
        members.append({'ContainerId': cid, 'ContainerName': name, 'ProductId': PRODUCT,
                        'Qty': qty, 'Qty2': '', 'ChangeCount': '1'})
        routes.append({'container': {'name': name, 'instanceId': cid},
                       'status': {'Operation': {'InstanceId': OPERATION}, 'Spec': {'InstanceId': SPEC},
                                  'Step': {'InstanceId': STEP}},
                       'source_step': {'instanceId': STEP}, 'path': {'instanceId': path},
                       'to_step': {'instanceId': target}, 'target_spec': {'instanceId': target_spec},
                       'reentry_step': {'instanceId': entry}, 'end_rework': {'InstanceId': end} if end else None})
        env['mainlines'].append(template | {'HistoryMainlineId': mid, 'ContainerId': cid, 'HistoryId': cid,
            'ContainerName': name, 'TxnType': '6660', 'TxnTypeName': 'Rework', 'InRework': 'true', 'Comments': '',
            'Qty': qty, 'Qty2': '', 'ToQty': qty, 'ToQty2': ''})
        env['moves'].append({'MoveHistoryId': move_id, 'HistoryMainlineId': mid, 'HistoryId': cid, 'TxnId': TXN,
            'CDOTypeId': '6700', 'ProductId': PRODUCT, 'PathId': path, 'StepId': STEP, 'ToStepId': target,
            'ToSpecId': target_spec, 'ReEntryStepId': entry, 'EndReworkStepId': end, 'ReworkReasonId': REASON,
            'Qty': qty, 'Qty2': '', 'CloseFlag': 'false', 'isFailed': 'false'})
    snap.update(kind='lot_rework', action='rework', service='ExecuteSamplingLot', name='L', target_id=lot,
                before={'members': members}, preview={'member_routes': routes},
                body={'samplingLot': {'name': 'L', 'instanceId': lot}, 'samplingLotResult': 2,
                      'reworkReason': snap['reason'], 'comments': root['Comments']})
    return env


def test_batch_rework_attributes_routes_and_preserves_manual_review(rework):
    digest = install(rework)
    candidates = json.loads(asyncio.run(history.find_quality_execution_candidates('plan',digest)))
    assert [row['TxnId'] for row in candidates['rows']] == [TXN]
    assert not candidates['target_linkage_available']
    result = reconcile(digest)
    assert result['comparison'] == 'native_history_match', result
    assert result['request_correlation_verified'] and result['recorded_business_effects_verified']
    assert not result['verified_success'] and not result['completion_causal_chain_verified']
    assert not result['retry_allowed'] and not result['state_changed']
    with plans.db_connect() as db:
        assert db.execute('SELECT state,outcome FROM plans').fetchone() == ('unknown',None)
        assert not db.execute('SELECT 1 FROM reviews').fetchall()


@pytest.mark.parametrize(('table','index','field','bad'), [
    ('lot',0,'HistoryId',key(999)), ('lot',0,'SamplingLotResult','3'), ('lot',0,'ReworkReasonId',key(999)),
    ('mainlines',1,'ReversalStatus','2'), ('mainlines',1,'InRework','false'), ('mainlines',1,'ToQty','0'),
    ('mainlines',1,'ContainerChangeCount','2'), ('mainlines',1,'IntegrationTxnId',key(999)),
    ('moves',0,'HistoryMainlineId',key(51)), ('moves',0,'HistoryId',key(50)), ('moves',0,'PathId',key(999)),
    ('moves',0,'ToStepId',key(999)), ('moves',0,'ToSpecId',key(999)), ('moves',0,'ReEntryStepId',key(999)),
    ('moves',1,'EndReworkStepId',STEP), ('moves',0,'ReworkReasonId',key(999)), ('moves',0,'Qty2','1'),
    ('moves',0,'CloseFlag','true'), ('moves',0,'isFailed','true'),
])
def test_batch_rework_rejects_conflicting_native_history(rework, table, index, field, bad):
    rework[table][index][field] = bad
    result = reconcile(install(rework))
    assert result['comparison'] == 'mismatch', result
    assert not result['request_correlation_verified'] and not result['recorded_business_effects_verified']


@pytest.mark.parametrize('case', ['missing_member', 'missing_move', 'extra_move', 'quantity_adjustment', 'changed_move'])
def test_batch_rework_rejects_incomplete_or_unstable_transactions(rework, case):
    if case == 'missing_member':
        rework['mainlines'].pop()
    elif case == 'missing_move':
        rework['moves'].pop()
    elif case == 'extra_move':
        rework['moves'].append(rework['moves'][0] | {'MoveHistoryId': key(999)})
    elif case == 'quantity_adjustment':
        rework['summaries'].append({'QtyHistoryId': SUMMARY, 'TxnId': TXN})
    else:
        rework['changed'] = 'moves'
    assert not reconcile(install(rework))['request_correlation_verified']


@pytest.mark.parametrize('case', ['match', 'wrong_lot', 'wrong_result', 'other_reason', 'reversed', 'changed'])
def test_batch_ok_uses_native_lot_identity_without_claiming_member_effects(env, case):
    lot = key(30)
    snap = env['snapshot']
    root = env['roots'][0] | {'ContainerId': '', 'HistoryId': '', 'TxnTypeName': 'ExecuteSamplingLot', 'TxnType': '8980'}
    env.update(roots=[root], mainlines=[root])
    env['lot'] = [{'ExecuteSamplingLotHistoryId': key(31), 'HistoryMainlineId': ROOT, 'HistoryId': lot, 'TxnId': TXN,
                   'IntegrationTxnId': ROOT, 'CDOTypeId': '8989', 'SamplingLotResult': '1', 'SamplingCompleted': 'true',
                   'ScrapReasonId': '', 'ReworkReasonId': ''}]
    snap.update(kind='lot_completion', action='ok', service='ExecuteSamplingLot', name='L', target_id=lot,
                body={'samplingLot': {'name': 'L', 'instanceId': lot}, 'samplingCompleted': True,
                      'samplingLotResult': 1, 'comments': root['Comments']})
    if case == 'wrong_lot':
        env['lot'][0]['HistoryId'] = key(999)
    elif case == 'wrong_result':
        env['lot'][0]['SamplingLotResult'] = '2'
    elif case == 'other_reason':
        env['lot'][0]['ReworkReasonId'] = REASON
    elif case == 'reversed':
        root['ReversalStatus'] = '2'
    elif case == 'changed':
        env['changed'] = 'lot'
    digest = install(env)
    result = reconcile(digest)
    assert result['request_correlation_verified'] is (case == 'match'), result
    assert not result.get('recorded_business_effects_verified') and not result['completion_causal_chain_verified']
    assert not result['verified_success'] and not result['state_changed']
    assert 'error' in history_review(digest)
    assert all('QtyHistory' not in query for query in env['queries'])


def history_review(digest, txn=TXN):
    return json.loads(asyncio.run(plans.review_quality_transaction(
        'plan', digest, 'resolve_history', txn, '核对原事务与当前原对象效果。', 'committed')))


@pytest.mark.parametrize('case', ['match', 'wrong_current_qty', 'current_changed', 'wrong_txn', 'reversed', 'in_flight',
                                  'audit_write_failure'])
def test_loss_final_audit_is_atomic_and_requires_stable_current_effects(env, monkeypatch, case):
    from tools import quality_quantity_loss as loss
    before = env['snapshot']['before']['container']
    observed = before | {'Qty': '8', 'Qty2': '4', 'Status': 1, 'ChangeCount': 2}
    reads = 0

    async def record(cid):
        nonlocal reads
        assert cid == CONTAINER
        reads += 1
        if case == 'wrong_current_qty' or case == 'current_changed' and reads == 2:
            return observed | {'Qty': '7'}
        return observed

    monkeypatch.setattr(loss, 'record', record)
    digest = install(env, state='in_flight' if case == 'in_flight' else 'unknown')
    if case == 'reversed':
        env['roots'][0]['ReversalStatus'] = '2'
    if case == 'audit_write_failure':
        with plans.db_connect() as db:
            db.execute("CREATE TRIGGER fail_state BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT,'fault'); END")
        with pytest.raises(sqlite3.IntegrityError):
            history_review(digest)
    else:
        out = history_review(digest, key(99) if case == 'wrong_txn' else TXN)
        assert out['state_changed'] is (case == 'match'), out
        if case == 'match':
            assert out['state'] == 'history_committed' and out['verified_success']
            assert not out['mes_write_performed'] and not out['original_plan_reusable']
            assert out['history_attestation']['candidate_txn_id'] == TXN
            assert out['history_attestation']['plan_digest'] == digest
            assert len(out['history_attestation']['evidence_digest']) == 64
            assert 'error' in history_review(digest)
    with plans.db_connect() as db:
        state = db.execute('SELECT state FROM plans').fetchone()[0]
        records = db.execute('SELECT record FROM reviews').fetchall()
        assert state == ('history_committed' if case == 'match' else 'in_flight' if case == 'in_flight' else 'unknown')
        assert len(records) == (1 if case == 'match' else 0)


@pytest.mark.parametrize('case', ['match', 'missing_baseline', 'retained_task', 'new_task', 'route_unavailable'])
def test_batch_rework_final_audit_requires_original_routes_and_task_cleanup(rework, monkeypatch, case):
    from tools import quality_lot_rework
    snap = rework['snapshot']
    current = [row | {'Status': '1', 'ChangeCount': '2'} for row in snap['before']['members']]
    evidence = {
        'current_lot': {'lot': {'MfgLotId': snap['target_id'], 'SamplingCompleted': True, 'SamplingLotResult': 2}},
        'original_members_current_records': current,
        'missing_original_member_ids': [],
        'member_route_evidence': [{'container_id': row['ContainerId'], 'available': case != 'route_unavailable',
                                   'expected_route_currently_observed': True, 'quantity_currently_unchanged': True}
                                  for row in current],
        'sampling_task_cleanup': {'baseline_available': case != 'missing_baseline',
                                  'all_original_tasks_absent': case != 'retained_task',
                                  'new_tasks': [{}] if case == 'new_task' else []},
    }

    async def inspect(snapshot):
        assert snapshot == snap
        return evidence

    monkeypatch.setattr(quality_lot_rework, 'inspect_snapshot', inspect)
    out = history_review(install(rework))
    assert out['state_changed'] is (case == 'match'), out
